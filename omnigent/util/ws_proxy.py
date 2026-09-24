"""Mandatory-egress proxy support for omnigent's own WebSocket tunnels.

Inside a sandbox whose only network path is a CONNECT proxy
(``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` set, no direct DNS or TCP
egress), every proxy-honoring HTTP client reaches the server fine, but the
pinned ``websockets<15`` client has no proxy support at all: ``connect()``
dials the origin directly and loops on "Temporary failure in name
resolution" forever, so the host and runner tunnels never come up.

This module gives those tunnels the same env-proxy semantics as the HTTP
clients: :func:`ws_env_proxy_url` picks the proxy the standard environment
variables configure for a ``ws(s)://`` URL (honoring ``NO_PROXY``), and
:func:`open_proxy_connect_socket` establishes the CONNECT tunnel so the
connected socket can be handed to ``websockets`` via its ``sock=``
parameter. No dependency bump is required, nothing changes when no proxy
is configured, and the explicit socket keeps working if the ``websockets``
pin is ever lifted.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import socket
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

_logger = logging.getLogger(__name__)

# ws:// upgrades ride plain HTTP and wss:// rides TLS — match the proxy
# variable an HTTP client uses for the same origin, with all_proxy fallback.
_PROXY_ENV_BY_WS_SCHEME = {"ws": "http_proxy", "wss": "https_proxy"}

_DEFAULT_PORT_BY_WS_SCHEME = {"ws": 80, "wss": 443}

# CONNECT responses are a handful of header lines; anything bigger is a
# confused intermediary, not a proxy.
_MAX_CONNECT_RESPONSE_BYTES = 65536

# Proxy schemes the CONNECT dialer speaks. SOCKS / TLS-to-proxy URLs are
# warned about once and ignored (direct dial preserves prior behavior).
_SUPPORTED_PROXY_SCHEMES = frozenset({"http"})
_warned_unsupported_schemes: set[str] = set()


def _env(environ: Mapping[str, str], name: str) -> str | None:
    """Read a proxy env var, preferring the conventional lowercase form.

    :param environ: Environment mapping to read.
    :param name: Lowercase variable name, e.g. ``"http_proxy"``.
    :returns: The first non-empty value of ``name``/``NAME``, or None.
    """
    for key in (name, name.upper()):
        value = environ.get(key)
        if value:
            return value
    return None


def _bypassed_by_no_proxy(host: str, port: int, no_proxy: str) -> bool:
    """Whether ``no_proxy`` exempts *host*:*port* from proxying.

    Standard comma-separated entries: ``*`` disables proxying entirely; a
    plain name matches itself and its subdomains (a leading dot is
    equivalent); a ``host:port`` entry additionally requires the port.

    :param host: Target hostname (no brackets), lowercase or not.
    :param port: Target port.
    :param no_proxy: Raw ``no_proxy`` value.
    :returns: True when the target must be dialed directly.
    """
    host = host.lower().rstrip(".")
    for raw_entry in no_proxy.split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        entry_port: int | None = None
        if entry.startswith("["):
            # Bracketed IPv6, optionally with a port.
            bracket_host, _, rest = entry.partition("]")
            entry_host = bracket_host[1:]
            if rest.startswith(":") and rest[1:].isdigit():
                entry_port = int(rest[1:])
        elif entry.count(":") == 1 and entry.rsplit(":", 1)[1].isdigit():
            entry_host, port_text = entry.rsplit(":", 1)
            entry_port = int(port_text)
        else:
            # Plain hostname or a bare IPv6 literal like ``::1``.
            entry_host = entry
        # "*.example.com", ".example.com", and "example.com" all mean the
        # domain and its subdomains.
        entry_host = entry_host.lstrip("*").lstrip(".").rstrip(".")
        if not entry_host:
            continue
        if entry_port is not None and entry_port != port:
            continue
        if host == entry_host or host.endswith("." + entry_host):
            return True
    return False


def ws_env_proxy_url(ws_url: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Return the CONNECT proxy URL the environment mandates for *ws_url*.

    Mirrors the standard env semantics the host's own HTTP calls already
    honor: ``ws://`` follows ``http_proxy``, ``wss://`` follows
    ``https_proxy``, both fall back to ``all_proxy``, and ``no_proxy``
    bypasses matching hosts.

    :param ws_url: Tunnel URL, e.g. ``"wss://server/v1/hosts/h/tunnel"``.
    :param environ: Environment mapping (defaults to ``os.environ``).
    :returns: The proxy URL to CONNECT through, or None to dial direct
        (no proxy configured, target bypassed, or unsupported scheme).
    """
    if environ is None:
        environ = os.environ
    parts = urlsplit(ws_url)
    scheme = (parts.scheme or "").lower()
    proxy_env = _PROXY_ENV_BY_WS_SCHEME.get(scheme)
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if proxy_env is None or not host:
        return None
    proxy = _env(environ, proxy_env) or _env(environ, "all_proxy")
    if not proxy:
        return None
    no_proxy = _env(environ, "no_proxy")
    if no_proxy and _bypassed_by_no_proxy(
        host, port or _DEFAULT_PORT_BY_WS_SCHEME[scheme], no_proxy
    ):
        return None
    if "://" not in proxy:
        # Bare host:port proxy values are conventionally plain HTTP.
        proxy = f"http://{proxy}"
    proxy_scheme = (urlsplit(proxy).scheme or "").lower()
    if proxy_scheme not in _SUPPORTED_PROXY_SCHEMES:
        if proxy_scheme not in _warned_unsupported_schemes:
            _warned_unsupported_schemes.add(proxy_scheme)
            _logger.warning(
                "Ignoring %s proxy scheme %r for WebSocket tunnels: only http:// "
                "CONNECT proxies are supported; dialing direct",
                proxy_env,
                proxy_scheme,
            )
        return None
    return proxy


def redact_proxy_url(proxy_url: str) -> str:
    """Return *proxy_url* with any userinfo credentials removed, for logging.

    :param proxy_url: Proxy URL, possibly carrying ``user:pass@``.
    :returns: The URL without credentials.
    """
    parts = urlsplit(proxy_url)
    if "@" not in parts.netloc:
        return proxy_url
    return proxy_url.replace(parts.netloc, parts.netloc.rpartition("@")[2], 1)


async def open_proxy_connect_socket(
    proxy_url: str, ws_url: str, *, timeout: float
) -> socket.socket:
    """Establish a CONNECT tunnel to *ws_url*'s origin through *proxy_url*.

    The proxy resolves the target's name itself, so this works where the
    dialing process has no direct DNS or TCP path. TLS for ``wss://`` is
    layered on top of the returned socket by the caller's ``connect()``.

    :param proxy_url: HTTP proxy URL from :func:`ws_env_proxy_url`.
    :param ws_url: Tunnel URL whose origin the proxy should reach.
    :param timeout: Per-operation socket timeout for the dial + handshake.
    :returns: The connected socket, ready for ``websockets``' ``sock=``.
    :raises OSError: When the proxy is unreachable, refuses the CONNECT,
        or answers with something other than HTTP.
    """
    proxy = urlsplit(proxy_url)
    target = urlsplit(ws_url)
    if not proxy.hostname:
        raise OSError(f"proxy URL has no host: {redact_proxy_url(proxy_url)!r}")
    if not target.hostname:
        raise OSError(f"tunnel URL has no host: {ws_url!r}")
    target_port = target.port or _DEFAULT_PORT_BY_WS_SCHEME.get((target.scheme or "").lower(), 80)
    auth_header: str | None = None
    if proxy.username is not None:
        credentials = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}"
        auth_header = "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    return await asyncio.to_thread(
        _connect_sync,
        proxy.hostname,
        proxy.port or 80,
        target.hostname,
        target_port,
        auth_header,
        timeout,
    )


def _connect_sync(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    auth_header: str | None,
    timeout: float,
) -> socket.socket:
    """Blocking CONNECT handshake (run off-loop via ``asyncio.to_thread``).

    :param proxy_host: Proxy hostname or address.
    :param proxy_port: Proxy port.
    :param target_host: Origin hostname the proxy must reach.
    :param target_port: Origin port.
    :param auth_header: Optional ``Proxy-Authorization`` value.
    :param timeout: Socket timeout for the dial and each handshake read.
    :returns: The connected socket with its timeout cleared.
    :raises OSError: On dial failure or a refused/garbled CONNECT.
    """
    authority = f"[{target_host}]" if ":" in target_host else target_host
    authority = f"{authority}:{target_port}"
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        request_lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
        if auth_header is not None:
            request_lines.append(f"Proxy-Authorization: {auth_header}")
        sock.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1"))
        response = b""
        while b"\r\n\r\n" not in response:
            if len(response) > _MAX_CONNECT_RESPONSE_BYTES:
                raise OSError(f"proxy sent an oversized response to CONNECT {authority}")
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError(f"proxy closed the connection during CONNECT to {authority}")
            response += chunk
        head, _, residue = response.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        status_parts = status_line.split(" ", 2)
        status_code = (
            int(status_parts[1]) if len(status_parts) >= 2 and status_parts[1].isdigit() else 0
        )
        if not 200 <= status_code < 300:
            raise OSError(f"proxy refused CONNECT to {authority}: {status_line!r}")
        if residue:
            # WebSocket is client-first: the origin cannot have spoken yet, so
            # early bytes mean a confused proxy — fail rather than desync.
            raise OSError(f"proxy sent unexpected bytes after CONNECT to {authority}")
        # The tunnel is long-lived; the caller's event loop manages it now.
        sock.settimeout(None)
        return sock
    except BaseException:
        with contextlib.suppress(OSError):
            sock.close()
        raise
