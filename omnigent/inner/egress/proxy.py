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
import binascii
import email.policy
import hmac
import ipaddress
import logging
import socket
import ssl
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlparse

from omnigent.inner.credential_proxy import (
    SYNTHETIC_CREDENTIAL_PREFIX,
    CredentialRewriteRule,
)
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

# Verbs that can never legitimately need an injected credential, so the
# proxy refuses to attach (or swap in) the real secret on them regardless
# of the allowlist. TRACE is a loopback diagnostic — the final recipient
# reflects the request back to the caller, so a credential injected here
# would be echoed straight into the sandbox. OPTIONS only negotiates
# capabilities and has no business carrying a bound-host secret. Keeping
# this independent of the allowlist means a permissive ``* host/**`` rule
# can't accidentally re-open the leak.
_CREDENTIAL_INJECTION_FORBIDDEN_METHODS = frozenset({"TRACE", "OPTIONS"})

# Verbs governed by ``Max-Forwards`` (RFC 7231 §5.1.2). A conformant
# intermediary must decrement the hop count on these before forwarding and
# answer directly once it reaches zero; the header is ignored on every
# other method.
_MAX_FORWARDS_METHODS = frozenset({"TRACE", "OPTIONS"})


def _parse_http_headers(headers_raw: bytes) -> Message:
    """
    Parse a raw HTTP request header block with the stdlib email parser.

    HTTP/1.1's header grammar descends from the RFC 822 message format,
    so the standard library's email parser — the same machinery
    :mod:`http.client` uses under the hood — tokenizes header blocks
    correctly: case-insensitive field names, surrounding whitespace,
    obsolete line folding, and repeated headers, none of which a
    hand-rolled ``split(b"\\r\\n")`` loop handles. ``policy.HTTP``
    round-trips with CRLF line separators and ``max_line_length=None``
    (no folding on output), preserving header order and values so a
    re-serialized block stays byte-faithful to what the client sent.

    :param headers_raw: Raw request header block (CRLF-separated,
        terminated by a blank line), e.g.
        ``b"Host: github.com\\r\\nUser-Agent: git/2\\r\\n\\r\\n"``. The
        request line and body are handled by the caller, not included
        here.
    :returns: The parsed message (headers only; an empty payload). Set
        headers via ``msg["Name"] = value`` / ``del msg["Name"]`` and
        re-serialize with ``msg.as_bytes(policy=email.policy.HTTP)``.
    """
    return BytesParser(policy=email.policy.HTTP).parsebytes(headers_raw)


@dataclass(frozen=True)
class _AuthRewriteResult:
    """Outcome of a credential-proxy ``Authorization`` rewrite pass.

    :param headers: The header block to forward upstream — rewritten
        when a synthetic placeholder matched, otherwise the input bytes
        unchanged.
    :param error: A human-readable reason to reject the request with
        ``403`` (a placeholder was sent to the wrong host or is unknown),
        or ``None`` when the request may proceed.
    """

    headers: bytes
    error: str | None


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
    :param credential_rewrites: Optional host-scoped real-credential
        rules. By default (swap-on-access) the proxy attaches the real
        credential to a bound-host request that carries no
        ``Authorization`` header — nothing credential-shaped ever enters
        the sandbox. A rule whose entry opted into ``inject_env`` also
        carries a synthetic placeholder; the proxy swaps that placeholder
        for the real secret and rejects the same placeholder sent to any
        other host with ``403`` (the cross-host leak guard).
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
        credential_rewrites: list[CredentialRewriteRule] | None = None,
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
        # Two indexes over the rewrite rules:
        #
        # - ``_cred_by_host``: the swap-on-access path. For a request to a
        #   bound host that carries no Authorization header, the proxy
        #   injects the real credential. Exactly one rule per host — the
        #   parser rejects duplicate-host bindings, so this map never
        #   silently drops a rule.
        # - ``_cred_by_synthetic``: the opt-in placeholder path. Only
        #   entries that injected an ``oa_cred_*`` env var register here;
        #   the synthetic is globally unique so it alone identifies the
        #   rule (and thus the bound host) for the swap + leak guard.
        self._cred_by_host: dict[str, CredentialRewriteRule] = {}
        self._cred_by_synthetic: dict[str, CredentialRewriteRule] = {}
        for rule in credential_rewrites or []:
            self._cred_by_host[rule.host.lower()] = rule
            if rule.synthetic is not None:
                self._cred_by_synthetic[rule.synthetic] = rule
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
        # In-flight connection handlers, tracked from accept time by
        # ``_client_connected`` (which creates the task itself rather than
        # letting ``asyncio.start_server`` wrap a coroutine in a Task we
        # never see). Without this, a handler parked in a readline/relay
        # read would outlive ``loop.stop()`` and surface as "Task was
        # destroyed but it is pending"; holding the handle lets
        # :meth:`stop` cancel and drain every handler before the loop closes.
        self._client_tasks: set[asyncio.Task[None]] = set()

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
        server = await asyncio.start_server(self._client_connected, host, 0)
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
        server = await asyncio.start_unix_server(self._client_connected, str(sock_path))
        self._servers.append(server)
        logger.info("Egress proxy listening on unix:%s", sock_path)

    async def stop(self) -> None:
        """Stop all proxy listeners and drain in-flight handlers."""
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers.clear()
        self._tcp_port = None
        # Cancel any connection handlers still parked in a read/relay so
        # they run their ``finally`` close blocks and reach a terminal
        # state before the owning loop is stopped, rather than being
        # abandoned mid-await (which surfaces as "Task was destroyed but
        # it is pending"). The listeners are already closed, so no new
        # handlers can appear; every accepted connection is tracked from
        # accept time (see ``_client_connected``), so this single snapshot
        # is complete.
        pending = [task for task in self._client_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._client_tasks.clear()
        logger.info("Egress proxy stopped")

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def _client_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Server accept callback — create and track the handler task.

        Passed to ``asyncio.start_server`` as a *plain* (non-coroutine)
        callback so asyncio does not wrap the handler itself: we create
        the task here and add it to :attr:`_client_tasks` synchronously,
        the instant the connection is accepted. That closes the race a
        self-registering handler would have — a handler whose task is
        scheduled but has not yet run its first line is invisible to a
        ``_client_tasks`` snapshot in :meth:`stop`, so it would leak past
        ``loop.stop()`` as "Task was destroyed but it is pending". Holding
        the handle from accept time means :meth:`stop` can always cancel
        it.

        :param reader: Stream reader for the accepted client connection,
            handed straight to :meth:`_handle_client`.
        :param writer: Stream writer for the accepted client connection,
            handed straight to :meth:`_handle_client`.
        """
        task = asyncio.ensure_future(self._handle_client(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

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
            # Task removal from ``_client_tasks`` is handled by the
            # done-callback wired in ``_client_connected``.
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

            # Forward a request line re-serialized from the parsed
            # method/path rather than the raw ``inner_first`` bytes, so
            # the upstream always receives exactly the (method, path)
            # the policy authorized. Mirrors the plain-HTTP path in
            # ``_handle_http`` (``relative_line``); closes the
            # policy-vs-forwarded byte differential.
            inner_request_line = f"{inner_method} {inner_path} HTTP/1.1\r\n".encode("latin-1")

            # Max-Forwards conformance (RFC 7231 §5.1.2): terminate an
            # exhausted TRACE / OPTIONS at the proxy over the MITM tunnel
            # (never forwarding it into the credential-injection path);
            # decrement a positive budget on the forwarded headers.
            terminate, inner_headers_raw = self._apply_max_forwards(
                inner_method, inner_headers_raw
            )
            if terminate:
                logger.info(
                    "MAX-FORWARDS-TERMINATE %s https://%s%s", inner_method, host, inner_path
                )
                await self._send_max_forwards_reply(
                    tls_writer, inner_method, inner_request_line, inner_headers_raw
                )
                return

            content_length = int(inner_headers.get("content-length", "0"))
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(tls_reader.readexactly(content_length), timeout=30)

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
        # Swap any synthetic credential placeholder for the real secret,
        # bound to this host (rejects cross-host replay with 403).
        rewrite = self._rewrite_authorization(method=method, host=host, headers_raw=headers_raw)
        if rewrite.error is not None:
            logger.warning(
                "BLOCKED-CREDENTIAL %s https://%s%s — %s", method, host, path, rewrite.error
            )
            await self._send_forbidden(client_writer, rewrite.error)
            return
        # Single-shot the upstream so ``_relay_response`` gets a prompt EOF
        # instead of blocking on a keep-alive socket — and so pipelining
        # clients (git's libcurl) don't stall waiting to reuse a tunnel the
        # proxy only services once. See ``_force_connection_close``.
        headers_raw = self._force_connection_close(rewrite.headers)
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

        # Max-Forwards conformance (RFC 7231 §5.1.2): a TRACE / OPTIONS
        # request whose hop budget is exhausted terminates at the proxy —
        # answered here as the final recipient, never forwarded (and so
        # never reaching the credential-injection path). A positive budget
        # is decremented on the forwarded header block.
        terminate, headers_raw = self._apply_max_forwards(method, headers_raw)
        if terminate:
            logger.info("MAX-FORWARDS-TERMINATE %s http://%s%s", method, host, path)
            await self._send_max_forwards_reply(
                writer, method, f"{method} {path} HTTP/1.1\r\n".encode("latin-1"), headers_raw
            )
            return

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
        rewrite = self._rewrite_authorization(method=method, host=host, headers_raw=headers_raw)
        if rewrite.error is not None:
            logger.warning(
                "BLOCKED-CREDENTIAL %s http://%s%s — %s", method, host, path, rewrite.error
            )
            await self._send_forbidden(writer, rewrite.error)
            return
        # Single-shot the upstream (prompt EOF for the relay, no stalled
        # tunnel reuse). See ``_force_connection_close``.
        headers_raw = self._force_connection_close(rewrite.headers)

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
        """
        Parse raw header bytes into a lowercase-keyed dict.

        :param raw: Raw request header block (CRLF-separated,
            terminated by a blank line), e.g.
            ``b"Host: github.com\\r\\nContent-Length: 12\\r\\n\\r\\n"``.
        :returns: A dict mapping each lowercased header name to its
            value (last value wins on repeats), e.g.
            ``{"host": "github.com", "content-length": "12"}``.
        """
        msg = _parse_http_headers(raw)
        # Last value wins on repeated header names, matching the prior
        # hand-rolled parser; the only consumer reads single-valued
        # headers (e.g. ``content-length``).
        return {key.lower(): value for key, value in msg.items()}

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

    def _rewrite_authorization(
        self, *, method: str, host: str, headers_raw: bytes
    ) -> _AuthRewriteResult:
        """
        Attach the real credential to a bound-host request.

        Two paths, both keyed on the request host:

        - **Swap-on-access (default).** When a rule binds *host* and the
          request carries no ``Authorization`` header, the proxy injects
          ``Authorization: <scheme> <real>`` on the way out. The sandbox
          never held anything credential-shaped.
        - **Placeholder swap (opt-in).** When the request's
          ``Authorization`` header carries one of this proxy's synthetic
          placeholders (recognised by
          :data:`~omnigent.inner.credential_proxy.SYNTHETIC_CREDENTIAL_PREFIX`
          across the ``Basic`` / ``Bearer`` / ``token`` schemes), the
          header is rewritten to the real credential. A placeholder that
          is unknown or bound to a *different* host is rejected with
          ``403`` — the cross-host leak guard for a compromised sandbox.

        A non-synthetic ``Authorization`` header the client set itself is
        left untouched (and suppresses injection), so the proxy never
        clobbers an unrelated credential a tool deliberately sent.

        No injection or swap happens on the loopback/diagnostic verbs in
        :data:`_CREDENTIAL_INJECTION_FORBIDDEN_METHODS` (``TRACE`` /
        ``OPTIONS``) — TRACE would reflect the injected secret back into
        the sandbox, and neither verb can legitimately carry a bound-host
        credential. The headers are forwarded untouched so any synthetic
        placeholder the sandbox holds (a harmless fake) passes through
        without being upgraded to the real secret.

        :param method: HTTP method (case-insensitive), e.g. ``"GET"``.
        :param host: Upstream request host (case-insensitive).
        :param headers_raw: Raw HTTP header block (CRLF-separated).
        :returns: An :class:`_AuthRewriteResult` carrying either the
            (possibly rewritten / injected) headers or a rejection reason.
        """
        if method.upper() in _CREDENTIAL_INJECTION_FORBIDDEN_METHODS:
            # Independent of the allowlist and of whether a rule binds this
            # host: these verbs never receive the real credential.
            return _AuthRewriteResult(headers=headers_raw, error=None)
        if not self._cred_by_host:
            return _AuthRewriteResult(headers=headers_raw, error=None)

        host_key = host.lower()
        host_rule = self._cred_by_host.get(host_key)
        msg = _parse_http_headers(headers_raw)
        existing = msg.get_all("Authorization")
        changed = False
        if existing:
            rewritten: list[str] = []
            for value in existing:
                synthetic = self._extract_synthetic(value)
                if synthetic is None:
                    # A real/foreign Authorization the client set itself —
                    # leave it alone (and don't also inject over it).
                    rewritten.append(value)
                    continue
                rule = self._cred_by_synthetic.get(synthetic)
                if rule is None or rule.host != host_key:
                    # A value carrying our placeholder prefix that we don't
                    # recognise for this host. Refuse rather than forward —
                    # this is the leak guard for a compromised sandbox that
                    # tries to send a placeholder to an attacker host.
                    return _AuthRewriteResult(
                        headers=headers_raw,
                        error="synthetic credential is not allowed for this host",
                    )
                rewritten.append(self._format_real_auth(rule))
                changed = True
            if changed:
                del msg["Authorization"]
                for value in rewritten:
                    msg["Authorization"] = value
        elif host_rule is not None:
            # Swap-on-access: the request reached a bound host with no
            # Authorization header, so attach the real credential now.
            msg["Authorization"] = self._format_real_auth(host_rule)
            changed = True
        if not changed:
            # Nothing matched — forward the client's bytes untouched rather
            # than round-tripping them through the serializer.
            return _AuthRewriteResult(headers=headers_raw, error=None)
        return _AuthRewriteResult(headers=msg.as_bytes(policy=email.policy.HTTP), error=None)

    @staticmethod
    def _extract_synthetic(auth_value: str) -> str | None:
        """
        Extract a synthetic placeholder from an ``Authorization`` value.

        :param auth_value: The header value after ``Authorization:``,
            e.g. ``"Bearer oa_cred_..."`` or
            ``"Basic <base64(user:oa_cred_...)>"``.
        :returns: The placeholder string when the value carries one (in
            the Bearer/token token position or the Basic password field),
            else ``None``.
        """
        lowered = auth_value.lower()
        if lowered.startswith("basic "):
            encoded = auth_value[6:].strip()
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return None
            _, sep, password = decoded.partition(":")
            if not sep:
                return None
            candidate = password
        elif lowered.startswith("bearer "):
            candidate = auth_value[7:].strip()
        elif lowered.startswith("token "):
            candidate = auth_value[6:].strip()
        else:
            return None
        return candidate if candidate.startswith(SYNTHETIC_CREDENTIAL_PREFIX) else None

    @staticmethod
    def _format_real_auth(rule: CredentialRewriteRule) -> str:
        """
        Format the real ``Authorization`` value for a matched rule.

        Returns a ``str`` because the value is set on an
        :class:`email.message.Message` header, which the ``policy.HTTP``
        serializer renders back to bytes. All three schemes use the same
        encoding path: tokens / PATs are ASCII in practice, and Basic
        base64-encodes the ``username:secret`` pair as UTF-8 (permitted by
        RFC 7617), so there is no scheme-specific encoding divergence.

        :param rule: The matched synthetic-to-real rewrite rule.
        :returns: The header value string, e.g.
            ``"Bearer <real>"``, ``"token <real>"``, or
            ``"Basic <base64(username:real)>"``.
        :raises ValueError: If the rule carries an unsupported scheme.
        """
        if rule.scheme == "bearer":
            return f"Bearer {rule.real_secret}"
        if rule.scheme == "token":
            return f"token {rule.real_secret}"
        if rule.scheme == "basic":
            # The parser always populates ``username`` for Basic
            # bindings; fail loud rather than invent one if a malformed
            # rule reaches the emit path.
            if rule.username is None:
                raise ValueError(
                    f"basic credential rewrite for host {rule.host!r} is missing a username"
                )
            pair = f"{rule.username}:{rule.real_secret}".encode()
            return "Basic " + base64.b64encode(pair).decode("ascii")
        raise ValueError(f"unsupported credential rewrite scheme: {rule.scheme!r}")

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

    @staticmethod
    def _force_connection_close(headers_raw: bytes) -> bytes:
        """
        Force ``Connection: close`` on a request's forwarded header block.

        The proxy serves exactly one inner HTTP request per upstream
        connection and relays the reply by reading until EOF
        (:meth:`_relay_response`). An HTTP/1.1 upstream defaults to
        keep-alive and holds the socket open after the response, so the
        relay would otherwise block on its read until the 60 s timeout
        rather than finishing the moment the body is done.

        That stall also breaks clients that pipeline requests on a single
        connection. ``git``'s libcurl sends its unauthenticated
        ``/info/refs`` probe, receives ``401``, then sends the
        authenticated retry **on the same tunnel**. Because the proxy is
        still parked in the relay read (no upstream EOF), it never reads
        git's second request and git hangs until the caller's timeout.
        ``curl`` happened to dodge this: it consumed a
        ``Content-Length``-framed body and exited while the orphaned relay
        read lingered invisibly.

        Rewriting the hop-by-hop connection headers to a single
        ``Connection: close`` makes the upstream close after one response
        (a prompt, clean EOF for the relay) and signals the client that
        the tunnel is single-shot, so git transparently opens a fresh
        ``CONNECT`` for its retry. ``Connection``, ``Proxy-Connection``,
        and ``Keep-Alive`` are all hop-by-hop (RFC 7230 §6.1), so dropping
        the client's originals and substituting our own is correct rather
        than merely expedient.

        :param headers_raw: Raw request header block (CRLF-separated,
            terminated by a blank line).
        :returns: The header block with any ``Connection`` /
            ``Proxy-Connection`` / ``Keep-Alive`` headers dropped and a
            single ``Connection: close`` appended in the header section.
        """
        msg = _parse_http_headers(headers_raw)
        # ``del`` removes every instance of each hop-by-hop header
        # (RFC 7230 §6.1) before we substitute our own single directive.
        del msg["Connection"]
        del msg["Proxy-Connection"]
        del msg["Keep-Alive"]
        msg["Connection"] = "close"
        return msg.as_bytes(policy=email.policy.HTTP)

    @staticmethod
    def _apply_max_forwards(method: str, headers_raw: bytes) -> tuple[bool, bytes]:
        """
        Enforce ``Max-Forwards`` (RFC 7231 §5.1.2) for TRACE / OPTIONS.

        ``Max-Forwards`` bounds how many intermediaries a TRACE or OPTIONS
        request may traverse. A conformant intermediary that receives one
        of these verbs MUST inspect the header before forwarding:

        - value ``0`` (or a malformed value): the proxy is the final
          recipient and MUST NOT forward — it answers directly.
        - value ``> 0``: decrement by one, then forward.

        The header is ignored on every other method, so those requests
        pass through untouched.

        :param method: HTTP method (case-insensitive), e.g. ``"TRACE"``.
        :param headers_raw: Raw request header block (CRLF-separated).
        :returns: ``(terminate, headers)``. ``terminate`` is ``True`` when
            the proxy must answer as the final recipient (the caller must
            not forward upstream); otherwise ``headers`` is the block to
            forward, with ``Max-Forwards`` decremented when it was present.
        """
        # Normalize here so the guard holds even if a future caller forgets
        # to upper-case the verb (both current callers already do).
        if method.upper() not in _MAX_FORWARDS_METHODS:
            return False, headers_raw
        msg = _parse_http_headers(headers_raw)
        raw = msg.get("Max-Forwards")
        if raw is None:
            # No hop limit expressed — forward as a transparent hop.
            return False, headers_raw
        try:
            remaining = int(raw.strip())
        except ValueError:
            # A malformed count is treated as exhausted: terminate at the
            # proxy rather than forward an ambiguous hop budget upstream.
            remaining = 0
        if remaining <= 0:
            return True, headers_raw
        del msg["Max-Forwards"]
        msg["Max-Forwards"] = str(remaining - 1)
        return False, msg.as_bytes(policy=email.policy.HTTP)

    @staticmethod
    def _build_trace_echo(request_line: bytes, headers_raw: bytes) -> bytes:
        """
        Reflect a TRACE request as a ``message/http`` payload.

        RFC 7231 §4.3.8 has the final recipient echo the received request
        so the client can see what arrived. We drop the headers that could
        carry a secret (``Authorization`` / ``Cookie`` /
        ``Proxy-Authorization``) so the reflection can never hand a
        credential back to the caller — the exact leak this hardening
        exists to prevent.

        :param request_line: The origin-form request line to echo, e.g.
            ``b"TRACE /path HTTP/1.1\\r\\n"``.
        :param headers_raw: Raw request header block (CRLF-separated).
        :returns: The ``message/http`` body: the request line followed by
            the (sensitive-stripped) header block.
        """
        msg = _parse_http_headers(headers_raw)
        del msg["Authorization"]
        del msg["Cookie"]
        del msg["Proxy-Authorization"]
        return request_line + msg.as_bytes(policy=email.policy.HTTP)

    async def _send_max_forwards_reply(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        request_line: bytes,
        headers_raw: bytes,
    ) -> None:
        """
        Answer a TRACE / OPTIONS request as the final recipient.

        Called when ``Max-Forwards`` reached zero and the proxy must halt
        the request instead of forwarding (see :meth:`_apply_max_forwards`).
        TRACE reflects the received message (``message/http``) with any
        sensitive headers stripped; OPTIONS reports the proxy's own
        communication options via ``Allow``.

        Any request body (e.g. an ``OPTIONS`` with ``Content-Length``) is
        intentionally left undrained: the reply sets ``Connection: close``
        and the caller tears the connection down, so leftover bytes are
        discarded rather than mistaken for a pipelined request.

        :param writer: Stream to write the response to (the plaintext
            client writer, or the MITM ``tls_writer`` inside CONNECT).
        :param method: ``"TRACE"`` or ``"OPTIONS"`` (upper-case).
        :param request_line: Origin-form request line, echoed for TRACE.
        :param headers_raw: Raw request header block, echoed for TRACE.
        """
        if method == "TRACE":
            body = self._build_trace_echo(request_line, headers_raw)
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: message/http\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n" + body
            )
        else:  # OPTIONS
            # This ``Allow`` list is intentionally static and proxy-scoped:
            # it advertises the verbs the proxy itself answers for as the
            # final recipient of a ``Max-Forwards: 0`` request, NOT the
            # origin's capabilities (which are never queried on this path).
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Allow: GET, HEAD, POST, PUT, DELETE, PATCH, OPTIONS, TRACE\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
        try:
            writer.write(resp)
            await writer.drain()
        except Exception:  # noqa: BLE001 — response write is best-effort
            pass
