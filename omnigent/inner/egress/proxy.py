"""Async MITM HTTP(S) proxy with egress rule enforcement.

The proxy intercepts HTTP and HTTPS traffic from the sandboxed helper,
checks each request against the configured :class:`EgressRule` list,
and either forwards or rejects with HTTP 403.

For HTTPS, the proxy performs a TLS man-in-the-middle using per-host
certificates signed by the CA from :mod:`~omnigent.inner.egress.ca`.

The proxy listens on a Unix socket (for hard enforcement via network
namespace isolation) and/or a TCP port (for soft enforcement or
testing).

Lifecycle::

    proxy = EgressProxy(rules, ca_cert_path, ca_key_path)
    port = await proxy.start_tcp()
    # OR
    await proxy.start_unix(socket_path)
    # ... agent runs ...
    await proxy.stop()
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import logging
import socket
import ssl
from pathlib import Path
from urllib.parse import urlparse

from omnigent.inner.egress.certs import HostCertCache
from omnigent.inner.egress.rules import (
    EgressRule,
    check_host,
    check_request,
    is_dns_safe_host,
)

logger = logging.getLogger(__name__)

_CONNECT_RESPONSE = b"HTTP/1.1 200 Connection Established\r\n\r\n"
_BUF_SIZE = 65536
_HEADER_MAX = 65536
# S6 (security): the smallest printable ASCII byte (SP). Any byte below
# this is a control byte and is rejected in the inner request line — see
# ``_handle_connect`` for the request-line-smuggling rationale.
_MIN_PRINTABLE_BYTE = 0x20

# S2 (security): CSP-internal endpoints that present as globally
# routable IPs but actually reach inside the cloud tenant. These slip
# past every RFC-based "block private IP" check because the IANA
# / RFC1918 / RFC6598 / link-local classifications don't cover them
# — the cloud vendor just picked a public-looking IP and routes it
# only inside their own network.
#
# Stealing creds from these endpoints is the canonical SSRF-to-cloud
# escalation pattern (see "Capital One AWS metadata breach" for the
# 169.254.169.254 prior art that's now industry-standard to block).
# We block them by default alongside the broad ``not is_global``
# check so agents can't reach them via DNS rebinding even when the
# rule layer permits a wildcard.
#
# Each entry is a ``IPv4Network`` / ``IPv6Network`` so a future
# range-shaped trap (e.g. a /24) can be added without restructuring
# the check.
_CLOUD_TRAP_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # Azure WireServer / IaaS guest agent / virtual public IP. A
    # public-looking IPv4 that Azure routes only inside the Azure
    # network — used for boot-time guest agent communication, host
    # DNS, health probes, and as the legacy IaaS metadata anchor.
    # See: https://learn.microsoft.com/en-us/azure/virtual-network/what-is-ip-address-168-63-129-16
    ipaddress.ip_network("168.63.129.16/32"),
)


class EgressProxy:
    """Asyncio-based MITM HTTP(S) proxy with rule enforcement.

    :param rules: Parsed egress rules. Empty list means deny-all.
    :param ca_cert_path: Path to the MITM CA certificate PEM.
    :param ca_key_path: Path to the MITM CA private key PEM.
    :param upstream_ca_bundle: Optional path to a CA bundle for
        verifying upstream TLS connections. Defaults to system CAs.
        This file is read EXACTLY ONCE here in ``__init__`` to build
        :attr:`_upstream_ssl_ctx`; it is NOT re-read per request. It
        therefore MUST be a host-side path the sandboxed agent cannot
        write — the controller passes the immutable bundle under
        ``~/.cache/omnigent-egress`` (never mounted into the sandbox),
        NOT the agent-writable scratch copy used for the in-sandbox
        ``SSL_CERT_FILE``. Passing a sandbox-writable path here would
        let the agent append its own CA (MITM of relayed upstream TLS)
        or truncate it (self-DoS).
    :param block_private_destinations: When ``True`` (the default),
        the proxy resolves the upstream host before opening the TCP
        connection and refuses to connect when any resolved IP is
        not globally routable (catches RFC1918, loopback, link-local,
        IPv6 ULA, CGNAT / RFC6598, IETF reserved blocks, TEST-NETs,
        benchmark range, multicast) or belongs to a known
        cloud-provider "trap" endpoint (see ``_CLOUD_TRAP_NETWORKS``,
        which currently lists Azure WireServer ``168.63.129.16`` —
        a public-looking IP that Azure routes only inside the
        tenant). Defends against DNS-rebinding attacks where the
        agent uses a permissive wildcard rule with a domain it
        controls that resolves to ``127.0.0.1`` (parent localhost
        services), ``10.x`` (VPC internals), ``169.254.169.254``
        (cloud IMDS), ``100.100.100.200`` (Alibaba IMDS), or
        ``168.63.129.16`` (Azure WireServer). Set to ``False`` for
        agents that legitimately reach intranet endpoints — wired
        through ``OSEnvSandboxSpec.egress_allow_private_destinations``
        so the opt-in is auditable in the spec.
    :param auth_token: When set, every inbound proxy connection MUST
        carry ``Proxy-Authorization: Basic base64("omnigent:<token>")``
        or it is rejected with ``407 Proxy Authentication Required``.
        Defends against same-UID cross-helper abuse on platforms
        without per-process network isolation (macOS ``darwin_seatbelt``
        — Linux ``bwrap`` is already protected by its own network
        namespace). The token is delivered to the helper via an
        inherited pipe file descriptor (see
        :func:`omnigent.inner.os_env._HelperProcessClient.
        _start_egress_proxy_locked`) and injected into the helper's
        ``HTTP_PROXY`` / ``HTTPS_PROXY`` env vars **in-process**
        after exec, so the token never appears in the execve-time
        kernel snapshot read by ``ps -E`` / ``sysctl
        KERN_PROCARGS2``. The header is stripped before forwarding
        upstream (``Proxy-Authorization`` is hop-by-hop per RFC 7235
        but be paranoid). ``None`` (the default) disables the check.
    """

    def __init__(
        self,
        rules: list[EgressRule],
        ca_cert_path: Path,
        ca_key_path: Path,
        *,
        upstream_ca_bundle: Path | None = None,
        block_private_destinations: bool = True,
        auth_token: str | None = None,
    ) -> None:
        self._rules = rules
        self._cert_cache = HostCertCache(ca_cert_path, ca_key_path)
        # Build the upstream TLS verification context ONCE, at
        # construction time, from the configured bundle path. The
        # previous implementation re-read ``cafile`` on every
        # ``_forward_https`` call; because the controller handed it a
        # copy of the bundle living in the sandbox-writable scratch
        # tmpdir, a sandboxed agent could append its own CA (so the
        # parent proxy trusts attacker-issued certs for allow-listed
        # upstreams) or truncate the file (self-DoS). Pinning the
        # context here — sourced from the host-only bundle the agent
        # cannot write — removes the per-request read of an
        # agent-controlled trust store entirely.
        if upstream_ca_bundle is not None:
            self._upstream_ssl_ctx = ssl.create_default_context(cafile=str(upstream_ca_bundle))
        else:
            self._upstream_ssl_ctx = ssl.create_default_context()
        self._block_private_destinations = block_private_destinations
        self._auth_token = auth_token
        # Precompute the expected header bytes ONCE so the per-request
        # comparison is a constant-time memcmp instead of repeating
        # the base64 round-trip on every connection. Stored as bytes
        # so we can ``hmac.compare_digest`` against the raw header
        # value lifted from the request without re-encoding.
        if auth_token is not None:
            self._expected_auth_value = b"Basic " + base64.b64encode(
                f"omnigent:{auth_token}".encode()
            )
        else:
            self._expected_auth_value = None
        self._servers: list[asyncio.AbstractServer] = []
        self._tcp_port: int | None = None

    @property
    def port(self) -> int:
        """The TCP port the proxy is listening on (if started via TCP).

        :raises RuntimeError: If the proxy was not started via
            :meth:`start_tcp`.
        """
        if self._tcp_port is None:
            raise RuntimeError("Proxy not started on TCP")
        return self._tcp_port

    async def start_tcp(self, host: str = "127.0.0.1") -> int:
        """Start listening on a TCP port and return the assigned port.

        :param host: Bind address, e.g. ``"127.0.0.1"``.
        :returns: The assigned port number.
        """
        server = await asyncio.start_server(self._handle_client, host, 0)
        addr = server.sockets[0].getsockname()
        self._tcp_port = addr[1]
        self._servers.append(server)
        logger.info("Egress proxy listening on %s:%d", host, self._tcp_port)
        return self._tcp_port

    async def start_unix(self, path: str | Path) -> None:
        """Start listening on a Unix socket.

        :param path: Filesystem path for the Unix socket. The parent
            directory must exist. Any existing socket at this path is
            removed first.
        """
        sock_path = Path(path)
        sock_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._handle_client, str(sock_path))
        self._servers.append(server)
        logger.info("Egress proxy listening on unix:%s", sock_path)

    async def stop(self) -> None:
        """Stop all proxy listeners."""
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers.clear()
        self._tcp_port = None
        logger.info("Egress proxy stopped")

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Dispatch incoming proxy connection."""
        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not first_line:
                return

            line = first_line.decode("latin-1", errors="replace").strip()
            parts = line.split()
            if len(parts) < 2:
                writer.close()
                return

            method = parts[0].upper()
            target = parts[1]

            headers = await self._read_headers(reader)

            # S4 (security): auth check runs BEFORE any rule check or
            # upstream connect — a peer that can't prove it's our
            # helper learns nothing about which hosts are allowed
            # and triggers no upstream traffic. Same 407 response
            # for missing and wrong tokens so a probe can't
            # distinguish "no auth configured" from "wrong token".
            if not self._check_proxy_auth(headers):
                logger.warning(
                    "REJECT-AUTH %s %s — missing or invalid Proxy-Authorization",
                    method,
                    target,
                )
                await self._send_proxy_auth_required(writer)
                return

            # Strip the Proxy-Authorization header before any code
            # path that forwards the header block upstream (plain HTTP
            # via _handle_http). CONNECT discards proxy_headers
            # entirely (it MITMs and re-reads inner headers), so it's
            # safe by construction there. Belt-and-braces: strip
            # for both branches so a future refactor that decides
            # to forward proxy_headers can't accidentally leak the
            # token to an external host.
            headers = self._strip_proxy_auth(headers)

            if method == "CONNECT":
                await self._handle_connect(writer, reader, target, headers)
            else:
                await self._handle_http(writer, reader, method, target, first_line, headers)
        except asyncio.TimeoutError:
            logger.debug("Client connection timed out")
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("Unexpected error in proxy handler")
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except Exception:  # noqa: BLE001 — client close is best-effort
                pass

    # ------------------------------------------------------------------
    # HTTPS (CONNECT) handling
    # ------------------------------------------------------------------

    async def _handle_connect(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,  # noqa: ARG002
        target: str,
        proxy_headers: bytes,  # noqa: ARG002
    ) -> None:
        """Handle CONNECT — TLS MITM, inspect inner HTTP, enforce rules."""
        host, port = self._parse_host_port(target, default_port=443)

        # S5 (security): hostname canonicalization. Reject any host
        # carrying a byte outside the DNS grammar ``[A-Za-z0-9.-]``
        # BEFORE any rule match or DNS lookup. This single allowlist
        # forecloses every parser-differential smuggling vector called
        # out by the Anthropic sandbox-runtime 0.0.43 fix: NUL bytes
        # (libc ``getaddrinfo`` truncation vs Python ``str.endswith``
        # differential — leaks data to the attacker's authoritative
        # nameserver via subdomain labels), percent-encoding
        # (``attacker.com%2e.allowed.com`` opens a client-vs-proxy
        # decoder differential), CRLF (HTTP header / request
        # smuggling), and any other non-DNS byte that could feed a
        # downstream parser quirk. Generic 403 body so a probing
        # attacker can't distinguish "invalid host" from "denied by
        # policy" via the response — same oracle hygiene as
        # ``_send_proxy_auth_required``.
        if not is_dns_safe_host(host):
            logger.warning(
                "REJECT-INVALID-HOST CONNECT %r — host contains "
                "characters outside the DNS grammar [A-Za-z0-9.-]",
                target,
            )
            await self._send_forbidden(writer, "host contains forbidden character")
            return

        if not check_host(self._rules, host):
            await self._send_forbidden(writer, f"Host {host!r} not allowed")
            return

        # S2 (security): destination check must run BEFORE the MITM
        # TLS handshake. Two reasons:
        #
        #   1. We don't want to mint a per-host cert and burn a TLS
        #      handshake for a request we're going to reject anyway.
        #
        #   2. The MITM cert for a literal IP destination
        #      (e.g. ``127.0.0.1``) doesn't include an ``IPAddress``
        #      SAN, so the client-side TLS verification fails before
        #      our post-tunnel check fires — the rejection then
        #      surfaces as ``SSLCertVerificationError`` rather than
        #      the meaningful ``403 Forbidden`` the operator needs
        #      to debug a misconfigured agent.
        #
        # ``_forward_https`` and ``_forward_http`` keep the check as
        # defense in depth: a future code path that synthesises an
        # upstream connect without going through CONNECT would still
        # be guarded.
        try:
            await self._assert_destination_allowed(host, port)
        except PermissionError as exc:
            logger.warning("BLOCKED-DEST CONNECT %s:%d - %s", host, port, exc)
            await self._send_forbidden(writer, str(exc))
            return

        writer.transport.pause_reading()

        writer.write(_CONNECT_RESPONSE)
        await writer.drain()

        ssl_ctx = self._cert_cache.get_ssl_context(host)

        # Wire the post-handshake reader / protocol *before* calling
        # ``start_tls`` and pass them in directly, rather than calling
        # ``start_tls(transport, transport.get_protocol(), ...)``
        # followed by a manual ``tls_transport.set_protocol(tls_protocol)``
        # / ``tls_protocol.connection_made(...)`` swap.
        #
        # The previous pattern had a flaky race: ``start_tls`` returns
        # the moment the TLS handshake completes, but the internal
        # ``SSLProtocol`` may already have buffered the client's first
        # application bytes (the inner ``GET ... HTTP/1.1`` line) and
        # delivered them to the *original* protocol (the one
        # ``transport.get_protocol()`` returned, which feeds the
        # plaintext ``reader`` we used to parse the CONNECT line). Any
        # bytes that arrived in the window between ``start_tls``
        # returning and ``set_protocol`` running were lost to the new
        # ``tls_reader``, so ``tls_reader.readline()`` blocked until
        # its 30 s timeout and the request silently died — surfacing
        # to the client as a torn TLS tunnel (``http.client.
        # RemoteDisconnected: Remote end closed connection without
        # response`` / ``curl: (52) Empty reply from server``). This
        # was the dominant flake in our HTTPS egress e2e tests.
        #
        # By passing the new protocol to ``start_tls`` directly, the
        # ``SSLProtocol`` wires it as its app-protocol before resuming
        # reads, so every decrypted byte lands in ``tls_reader`` from
        # the very first one.
        tls_reader = asyncio.StreamReader()
        tls_protocol = asyncio.StreamReaderProtocol(tls_reader)

        transport = writer.transport
        loop = asyncio.get_event_loop()
        try:
            tls_transport = await loop.start_tls(
                transport, tls_protocol, ssl_ctx, server_side=True
            )
        except (ssl.SSLError, ConnectionResetError, OSError) as exc:
            # WARNING (was DEBUG) so a client that drops mid-handshake
            # is visible without raising caplog levels. Broadened from
            # ``ssl.SSLError`` alone because a client TCP reset during
            # the handshake raises ``ConnectionResetError`` / generic
            # ``OSError`` out of ``start_tls``, which previously fell
            # through to the catch-all ``except Exception:
            # logger.exception(...)`` in ``_handle_client`` and
            # polluted logs with a tracebackful "Unexpected error".
            logger.warning(
                "TLS handshake failed for %s: %s: %s",
                host,
                type(exc).__name__,
                exc,
            )
            return

        tls_writer = asyncio.StreamWriter(tls_transport, tls_protocol, tls_reader, loop)

        try:
            inner_first = await asyncio.wait_for(tls_reader.readline(), timeout=30)
            if not inner_first:
                return

            inner_line = inner_first.decode("latin-1", errors="replace").strip()

            # S6 (security): a sandboxed agent controls these
            # MITM-decrypted bytes, so the policy parse and the bytes we
            # forward upstream MUST NOT be able to diverge. The primary
            # guard is re-serializing the forwarded request line from the
            # parsed (method, path) below — that makes the upstream
            # receive byte-for-byte what the policy authorized, no matter
            # how ``str.split()`` tokenized the line. (``str.split()`` with
            # no argument splits on *any* Unicode whitespace, which after
            # the ``latin-1`` decode includes not just bare
            # ``\r``/``\t``/``\v``/``\f`` but also NEL ``0x85`` and NBSP
            # ``0xa0`` — so a control-byte filter alone would be
            # insufficient.) As defense in depth we additionally reject
            # any control byte (< SP) here, which gives a clean 403 for
            # the classic bare-``\r``/``\t`` request-line smuggle instead
            # of silently normalizing it.
            if any(ord(ch) < _MIN_PRINTABLE_BYTE for ch in inner_line):
                logger.warning(
                    "REJECT-CONTROL-CHAR CONNECT %s — inner request line contains a control byte",
                    host,
                )
                await self._send_forbidden(
                    tls_writer, "inner request line contains forbidden character"
                )
                return

            inner_parts = inner_line.split()
            if len(inner_parts) < 2:
                return

            inner_method = inner_parts[0].upper()
            inner_path = inner_parts[1]

            inner_headers_raw = await self._read_headers(tls_reader)
            inner_headers = self._parse_header_dict(inner_headers_raw)

            if not check_request(self._rules, inner_method, host, inner_path):
                logger.warning("BLOCKED %s https://%s%s", inner_method, host, inner_path)
                msg = f"{inner_method} https://{host}{inner_path} denied by policy"
                await self._send_forbidden(tls_writer, msg)
                return

            logger.info("ALLOW %s https://%s%s", inner_method, host, inner_path)

            content_length = int(inner_headers.get("content-length", "0"))
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(tls_reader.readexactly(content_length), timeout=30)

            # Forward a request line re-serialized from the parsed
            # method/path rather than the raw ``inner_first`` bytes, so
            # the upstream always receives exactly the (method, path)
            # the policy authorized. Mirrors the plain-HTTP path in
            # ``_handle_http`` (``relative_line``); closes the
            # policy-vs-forwarded byte differential.
            inner_request_line = f"{inner_method} {inner_path} HTTP/1.1\r\n".encode("latin-1")

            await self._forward_https(
                tls_writer,
                host,
                port,
                inner_method,
                inner_path,
                inner_request_line,
                inner_headers_raw,
                body,
            )
        except asyncio.TimeoutError:
            # WARNING (was DEBUG) so this is visible without raising
            # caplog levels: with the protocol-swap race fixed above,
            # this branch now only fires on a genuine anomaly (TLS
            # handshake completed but the client never sent an HTTP
            # request inside the tunnel), not on the routine race.
            # Synthesise a 504 over the established TLS tunnel so the
            # client sees a real status instead of an empty reply.
            logger.warning(
                "Inner request timed out for %s after TLS handshake "
                "(client opened CONNECT but sent no HTTP request)",
                host,
            )
            await self._send_gateway_timeout(
                tls_writer,
                f"client sent no inner HTTP request to {host} within 30 s",
            )
        except Exception:
            logger.exception("Error handling CONNECT inner request for %s", host)
        finally:
            try:
                tls_writer.close()
                await asyncio.wait_for(tls_writer.wait_closed(), timeout=2)
            except Exception:  # noqa: BLE001 — TLS close is best-effort
                pass

    async def _forward_https(
        self,
        client_writer: asyncio.StreamWriter,
        host: str,
        port: int,
        method: str,
        path: str,
        request_line: bytes,
        headers_raw: bytes,
        body: bytes,
    ) -> None:
        """Open a real TLS connection to the target and relay."""
        try:
            pinned_ip = await self._assert_destination_allowed(host, port)
        except PermissionError as exc:
            logger.warning("BLOCKED-DEST https://%s:%d - %s", host, port, exc)
            await self._send_forbidden(client_writer, str(exc))
            return
        # Reuse the context pinned at construction. Do NOT rebuild from
        # a cafile here — that re-read of a (potentially sandbox-writable)
        # file on every request was the vulnerability this guards against.
        ssl_ctx = self._upstream_ssl_ctx
        # Connect to the IP pinned by the destination check (or the
        # hostname when private-destination blocking is disabled and
        # ``pinned_ip`` is None). Connecting to the pinned IP removes
        # the second, independent DNS lookup that ``open_connection``
        # would otherwise perform — the rebinding window this guard
        # exists to close. ``server_hostname=host`` keeps TLS SNI and
        # certificate verification bound to the original hostname.
        connect_host = pinned_ip or host
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    connect_host,
                    port,
                    ssl=ssl_ctx,
                    server_hostname=host,
                ),
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 — upstream connect failure maps to 502
            logger.warning("Cannot connect to %s:%d - %s", host, port, exc)
            await self._send_bad_gateway(client_writer, str(exc))
            return

        try:
            upstream_writer.write(request_line)
            upstream_writer.write(headers_raw)
            if body:
                upstream_writer.write(body)
            await upstream_writer.drain()

            bytes_relayed, relay_exc = await self._relay_response(upstream_reader, client_writer)
            if bytes_relayed == 0:
                # Upstream accepted the TLS connection but closed
                # without sending a single byte of HTTP response.
                # Without this branch the client would see ``curl: (52)
                # Empty reply from server`` — indistinguishable from a
                # hard proxy block. Synthesise a 502 so the client gets
                # a real HTTP status to act on, and log enough context
                # (EOF vs. specific exception, request bytes sent) to
                # diagnose flaky upstreams from the captured logs.
                cause = type(relay_exc).__name__ if relay_exc else "EOF"
                logger.warning(
                    "Upstream %s:%d closed without response "
                    "(method=%s path=%s, request_bytes=%d, cause=%s)",
                    host,
                    port,
                    method,
                    path,
                    len(request_line) + len(headers_raw) + len(body),
                    cause,
                )
                await self._send_bad_gateway(
                    client_writer,
                    f"upstream {host}:{port} closed without response (cause={cause})",
                )
        finally:
            try:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            except Exception:  # noqa: BLE001 — upstream close is best-effort
                pass

    # ------------------------------------------------------------------
    # Plain HTTP handling
    # ------------------------------------------------------------------

    async def _handle_http(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        method: str,
        url: str,
        request_line: bytes,  # noqa: ARG002
        headers_raw: bytes,
    ) -> None:
        """Handle plain HTTP proxy request."""
        # S5 (security): ``urlparse`` raises ``ValueError`` on a few
        # malformed-authority shapes (notably unbalanced brackets, e.g.
        # ``http://attacker.example.com[evil].allowed.com/``). Without
        # this catch the exception escapes ``_handle_http`` and the
        # bare ``except Exception`` in ``_handle_client`` closes the
        # connection silently — a malformed-URL request shouldn't
        # surface as a torn TCP connection that's indistinguishable
        # from a hard proxy block. Treat the same as any other
        # invalid-host case and reply with the canonical 403.
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            logger.warning("REJECT-MALFORMED-URL %s %r — urlparse: %s", method, url, exc)
            await self._send_forbidden(writer, "host contains forbidden character")
            return
        host = parsed.hostname or ""
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        # S5 (security): same hostname canonicalization defense as
        # ``_handle_connect`` — see the comment there for the full
        # list of parser-differential vectors a strict DNS-grammar
        # allowlist forecloses. ``urlparse`` preserves embedded NULs
        # and percent characters in ``.hostname``, so the check must
        # run BEFORE ``check_request`` (whose wildcard branch uses
        # ``str.endswith``) and BEFORE ``_assert_destination_allowed``
        # (whose ``getaddrinfo`` is the DNS-exfil channel).
        if not is_dns_safe_host(host):
            logger.warning(
                "REJECT-INVALID-HOST %s %r — host contains characters "
                "outside the DNS grammar [A-Za-z0-9.-]",
                method,
                url,
            )
            await self._send_forbidden(writer, "host contains forbidden character")
            return

        if not check_request(self._rules, method, host, path):
            logger.warning("BLOCKED %s http://%s%s", method, host, path)
            await self._send_forbidden(writer, f"{method} http://{host}{path} denied by policy")
            return

        logger.info("ALLOW %s http://%s%s", method, host, path)

        try:
            pinned_ip = await self._assert_destination_allowed(host, port)
        except PermissionError as exc:
            logger.warning("BLOCKED-DEST http://%s%s - %s", host, path, exc)
            await self._send_forbidden(writer, str(exc))
            return

        inner_headers = self._parse_header_dict(headers_raw)
        content_length = int(inner_headers.get("content-length", "0"))
        body = b""
        if content_length > 0:
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=30)

        relative_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1")

        # Connect to the IP pinned by the destination check (or the
        # hostname when blocking is disabled and ``pinned_ip`` is
        # None) to avoid a second, independent DNS lookup — the
        # rebinding window this guard exists to close. The original
        # ``Host:`` header in ``headers_raw`` is forwarded unchanged,
        # so virtual-host routing still works when connecting by IP.
        connect_host = pinned_ip or host
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(connect_host, port),
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 — upstream connect failure maps to 502
            logger.warning("Cannot connect to %s:%d - %s", host, port, exc)
            await self._send_bad_gateway(writer, str(exc))
            return

        try:
            upstream_writer.write(relative_line)
            upstream_writer.write(headers_raw)
            if body:
                upstream_writer.write(body)
            await upstream_writer.drain()

            bytes_relayed, relay_exc = await self._relay_response(upstream_reader, writer)
            if bytes_relayed == 0:
                cause = type(relay_exc).__name__ if relay_exc else "EOF"
                logger.warning(
                    "Upstream %s:%d closed without response "
                    "(method=%s path=%s, request_bytes=%d, cause=%s)",
                    host,
                    port,
                    method,
                    path,
                    len(relative_line) + len(headers_raw) + len(body),
                    cause,
                )
                await self._send_bad_gateway(
                    writer,
                    f"upstream {host}:{port} closed without response (cause={cause})",
                )
        finally:
            try:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            except Exception:  # noqa: BLE001 — upstream close is best-effort
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _read_headers(self, reader: asyncio.StreamReader) -> bytes:
        """Read header block until CRLFCRLF, return raw bytes."""
        buf = b""
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            buf += line
            if line == b"\r\n" or line == b"\n" or not line:
                break
            if len(buf) > _HEADER_MAX:
                break
        return buf

    @staticmethod
    def _parse_header_dict(raw: bytes) -> dict[str, str]:
        """Parse raw header bytes into a lowercase-keyed dict."""
        result: dict[str, str] = {}
        for line in raw.split(b"\r\n"):
            if b":" in line:
                key, _, val = line.partition(b":")
                result[key.decode("latin-1").strip().lower()] = val.decode("latin-1").strip()
        return result

    async def _assert_destination_allowed(self, host: str, port: int) -> str | None:
        """
        Resolve *host*, validate every resolved address, and return a
        single pinned IP the caller MUST connect to, when
        :attr:`_block_private_destinations` is set.

        Raises :exc:`PermissionError` if any resolved address is
        non-globally-routable, multicast, or matches a known CSP
        "trap" endpoint.

        Uses ``ipaddress.ip_address(...).is_global`` rather than the
        narrower ``is_private`` flag so the check picks up CGNAT /
        RFC 6598 (``100.64.0.0/10``) — notably Alibaba Cloud's IMDS
        at ``100.100.100.200`` — in addition to the usual private,
        loopback, link-local, ULA, reserved, and TEST-NET blocks
        ``is_private`` already covers. Multicast (``224.0.0.0/4`` /
        ``ff00::/8``) is still marked ``is_global=True`` by Python,
        so it's checked separately.

        Cloud "trap" endpoints (``_CLOUD_TRAP_NETWORKS``) are public-
        looking IPs that vendors route only inside their own tenant
        — currently just Azure WireServer ``168.63.129.16``. These
        leak metadata and serve as cloud-host control planes, so
        they're refused regardless of ``is_global``.

        **Fail closed + pin the IP (DNS-rebinding defense).** This
        method resolves the host ONCE and returns the validated IP so
        the caller connects to that exact address instead of letting
        ``asyncio.open_connection`` perform an independent second
        lookup. Without pinning, the check and the connect are two
        separate resolutions (check-then-use / TOCTOU): an attacker
        who controls DNS for an allowed/wildcard host could fail the
        first lookup and return a private IP on the second, slipping
        past the guard. A DNS resolution failure (``socket.gaierror``)
        or an empty result therefore raises :exc:`PermissionError`
        (fail closed) rather than allowing the connect to proceed.

        :param host: Upstream hostname or IP literal to resolve and
            validate, e.g. ``"api.example.com"`` or ``"203.0.113.7"``.
        :param port: Upstream TCP port, e.g. ``443``. Passed to
            ``getaddrinfo`` to select the service.
        :returns: The validated IP string the caller must connect to
            (e.g. ``"203.0.113.7"``) when blocking is enabled, or
            ``None`` when :attr:`_block_private_destinations` is
            ``False`` (caller connects to the hostname directly).
        :raises PermissionError: when blocking is enabled and the
            host fails to resolve, resolves to no usable address, or
            resolves to a non-public / multicast / cloud-trap address.
        """
        if not self._block_private_destinations:
            return None
        try:
            infos = await asyncio.get_event_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            # Fail closed. A re-resolution at connect time is the
            # rebinding channel this guard exists to close: if we
            # returned here, the subsequent ``asyncio.open_connection``
            # would resolve the name a second time and could land on a
            # private IP that this lookup never saw. Refuse instead of
            # deferring to the connect path.
            raise PermissionError(
                f"host {host!r} DNS resolution failed ({exc}) — blocked "
                "because egress_allow_private_destinations is False"
            ) from exc
        pinned_ip: str | None = None
        for family, _type, _proto, _canon, sockaddr in infos:
            if family == socket.AF_INET:
                ip_str = sockaddr[0]
            elif family == socket.AF_INET6:
                ip_str = sockaddr[0]
                # IPv6 stores the address as the first tuple element
                # already; strip any zone-id suffix like "%en0".
                if "%" in ip_str:
                    ip_str = ip_str.split("%", 1)[0]
            else:
                continue
            try:
                addr = ipaddress.ip_address(ip_str)
            except ValueError:
                # Unparseable address: refuse to connect. This is the
                # paranoid choice and matches deny-by-default.
                raise PermissionError(
                    f"unparseable address {ip_str!r} for host {host!r}"
                ) from None
            if not addr.is_global or addr.is_multicast:
                raise PermissionError(
                    f"host {host!r} resolves to non-public address "
                    f"{ip_str} — blocked because "
                    "egress_allow_private_destinations is False"
                )
            for trap_net in _CLOUD_TRAP_NETWORKS:
                if addr.version == trap_net.version and addr in trap_net:
                    raise PermissionError(
                        f"host {host!r} resolves to cloud-internal "
                        f"endpoint {ip_str} ({trap_net}) — blocked "
                        "because egress_allow_private_destinations "
                        "is False"
                    )
            # First address that passed every check becomes the pinned
            # connect target. We keep validating the rest so a list
            # mixing public and private addresses still fails closed.
            if pinned_ip is None:
                pinned_ip = ip_str
        if pinned_ip is None:
            # getaddrinfo returned only address families we don't
            # connect over (no AF_INET / AF_INET6 entry). Fail closed
            # rather than fall through to a hostname re-resolution.
            raise PermissionError(
                f"host {host!r} resolved to no usable IPv4/IPv6 address "
                "— blocked because egress_allow_private_destinations is False"
            )
        return pinned_ip

    @staticmethod
    def _parse_host_port(target: str, default_port: int = 443) -> tuple[str, int]:
        """Parse 'host:port' from a CONNECT target."""
        if ":" in target:
            host, port_str = target.rsplit(":", 1)
            try:
                return host, int(port_str)
            except ValueError:
                return target, default_port
        return target, default_port

    @staticmethod
    async def _relay_response(
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> tuple[int, BaseException | None]:
        """Stream the upstream response back to the client.

        Returns ``(bytes_written, exception)`` where ``bytes_written``
        is the total number of upstream bytes forwarded to the client
        and ``exception`` is the swallowed exception that ended the
        relay (or ``None`` on a clean EOF). ``bytes_written == 0``
        means the upstream produced no response at all — EOF on the
        first read or a connection error before any data arrived. The
        caller uses this to synthesise a 502 instead of letting the
        client see an empty stream (``curl: (52) Empty reply from
        server``). The exception is logged for diagnostics only.
        """
        bytes_written = 0
        exc: BaseException | None = None
        try:
            while True:
                data = await asyncio.wait_for(upstream_reader.read(_BUF_SIZE), timeout=60)
                if not data:
                    break
                client_writer.write(data)
                await client_writer.drain()
                bytes_written += len(data)
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError) as e:
            exc = e
        return bytes_written, exc

    @staticmethod
    async def _send_forbidden(writer: asyncio.StreamWriter, message: str) -> None:
        """Send HTTP 403 response."""
        body = f"403 Forbidden: {message}\r\n".encode()
        resp = (
            b"HTTP/1.1 403 Forbidden\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )
        try:
            writer.write(resp)
            await writer.drain()
        except Exception:  # noqa: BLE001 — response write is best-effort
            pass

    @staticmethod
    async def _send_bad_gateway(writer: asyncio.StreamWriter, message: str) -> None:
        """Send HTTP 502 response."""
        body = f"502 Bad Gateway: {message}\r\n".encode()
        resp = (
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )
        try:
            writer.write(resp)
            await writer.drain()
        except Exception:  # noqa: BLE001 — response write is best-effort
            pass

    @staticmethod
    async def _send_gateway_timeout(writer: asyncio.StreamWriter, message: str) -> None:
        """Send HTTP 504 response.

        Used after a CONNECT tunnel is established but the client
        never sends an inner HTTP request within the deadline. Writing
        the 504 over the (now TLS-wrapped) writer gives the client a
        real status to act on instead of a torn tunnel.
        """
        body = f"504 Gateway Timeout: {message}\r\n".encode()
        resp = (
            b"HTTP/1.1 504 Gateway Timeout\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )
        try:
            writer.write(resp)
            await writer.drain()
        except Exception:  # noqa: BLE001 — response write is best-effort
            pass

    @staticmethod
    async def _send_proxy_auth_required(writer: asyncio.StreamWriter) -> None:
        """Send HTTP 407 Proxy Authentication Required.

        The ``Proxy-Authenticate`` header advertises Basic with the
        realm ``omnigent`` so any HTTP client following RFC 7235
        will resend with credentials lifted from the proxy URL's
        userinfo component. Body is intentionally generic — leaking
        "you forgot the token" vs "you sent the wrong one" gives a
        probing same-UID attacker free oracle bits.
        """
        body = b"407 Proxy Authentication Required\r\n"
        resp = (
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b'Proxy-Authenticate: Basic realm="omnigent"\r\n'
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )
        try:
            writer.write(resp)
            await writer.drain()
        except Exception:  # noqa: BLE001 — response write is best-effort
            pass

    def _check_proxy_auth(self, headers_raw: bytes) -> bool:
        """
        Return ``True`` iff the request is allowed by the auth policy.

        - When :attr:`_auth_token` is ``None``, all requests pass (the
          proxy was constructed without auth — relied on by tests and
          by deployments that don't run the in-helper config FD path).
        - When set, the request MUST carry ``Proxy-Authorization:
          Basic <base64(omnigent:<token>)>``. Compared with
          :func:`hmac.compare_digest` so the time-to-mismatch doesn't
          leak the prefix.
        """
        if self._expected_auth_value is None:
            return True
        # Header parsing: walk CRLF-separated lines, case-insensitive
        # match on the field name. Don't trust ``_parse_header_dict``
        # here because we need the raw value with its exact byte
        # representation for the constant-time compare.
        target = b"proxy-authorization:"
        for line in headers_raw.split(b"\r\n"):
            if line[: len(target)].lower() == target:
                value = line[len(target) :].strip()
                return hmac.compare_digest(value, self._expected_auth_value)
        return False

    @staticmethod
    def _strip_proxy_auth(headers_raw: bytes) -> bytes:
        """
        Return *headers_raw* with any ``Proxy-Authorization`` line
        removed. The header is hop-by-hop (RFC 7235 §4) so MUST NOT
        be forwarded upstream, but we don't rely on the upstream
        server to honor that — we drop it ourselves so a logged
        upstream request can never accidentally carry the token to
        a remote host.
        """
        target = b"proxy-authorization:"
        kept: list[bytes] = []
        for line in headers_raw.split(b"\r\n"):
            if line[: len(target)].lower() == target:
                continue
            kept.append(line)
        return b"\r\n".join(kept)
