"""Mandatory-egress proxy support for omnigent's own WebSocket tunnels.

The pinned ``websockets<15`` client has no proxy support, so inside a sandbox
whose only network path is a CONNECT proxy the host and runner tunnels would
dial the origin directly and never come up. This module applies the proxy
environment the way the host's HTTP client (httpx) does, including
``NO_PROXY`` and the loopback exemption, and establishes the CONNECT tunnel
so the connected socket can be handed to ``websockets`` via ``sock=``.

A pre-connected socket means WebSocket-level redirects are not followed; the
tunnel endpoints never redirect, and HTTP(S) login redirects still surface as
``InvalidURI`` for the callers to classify.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import logging
import os
import socket
import time
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

from omnigent_client._http import is_loopback_url

_logger = logging.getLogger(__name__)

# ws:// upgrades ride plain HTTP and wss:// rides TLS — match the proxy
# variable an HTTP client uses for the same origin, with all_proxy fallback.
_PROXY_ENV_BY_WS_SCHEME = {"ws": "http_proxy", "wss": "https_proxy"}

_DEFAULT_PORT_BY_WS_SCHEME = {"ws": 80, "wss": 443}
_HTTP_SCHEME_BY_WS_SCHEME = {"ws": "http", "wss": "https"}
_DEFAULT_PORT_BY_HTTP_SCHEME = {"http": 80, "https": 443}

# CONNECT responses are a handful of header lines; anything bigger is a
# confused intermediary, not a proxy.
_MAX_CONNECT_RESPONSE_BYTES = 65536

# Proxy schemes the CONNECT dialer speaks. SOCKS / TLS-to-proxy URLs are
# warned about once per variable and ignored (direct dial preserves prior
# behavior), as are values that do not parse.
_SUPPORTED_PROXY_SCHEMES = frozenset({"http"})
_warned_proxy_env: set[tuple[str, str]] = set()


def _warn_once(key: tuple[str, str], message: str, *args: object) -> None:
    """Log *message* the first time *key* (variable, reason) is seen.

    :param key: The proxy variable and the reason it is being ignored.
    :param message: Logging format string.
    :param args: Format arguments.
    """
    if key not in _warned_proxy_env:
        _warned_proxy_env.add(key)
        _logger.warning(message, *args)


def _env(environ: Mapping[str, str], name: str) -> str | None:
    """Read a proxy variable with urllib's two-pass precedence, as httpx does.

    Any capitalisation of *name* counts; a spelling ending in lowercase
    ``_proxy`` takes precedence, and such a spelling that is present but empty
    suppresses the others (``http_proxy=""`` disables ``HTTP_PROXY``). Like
    urllib, ``HTTP_PROXY`` is ignored when ``REQUEST_METHOD`` marks a CGI
    environment, where it would carry the request's ``Proxy`` header.

    :param environ: Environment mapping to read.
    :param name: Lowercase variable name, e.g. ``"http_proxy"``.
    :returns: The selected value, or None when unset or suppressed.
    """
    value: str | None = None
    for key, candidate in environ.items():
        if key.lower() == name and candidate:
            value = candidate
    if name == "http_proxy" and "REQUEST_METHOD" in environ:
        value = None
    for key, candidate in environ.items():
        if key.lower() == name and key.endswith("_proxy"):
            value = candidate or None
    return value


def _bypassed_by_no_proxy(
    host: str, port: int | None, no_proxy: str, http_scheme: str, proxy_from_all: bool
) -> bool:
    """Whether ``no_proxy`` exempts the target from proxying, as httpx would.

    Mirrors the rules the host's own HTTP client applies so HTTP requests and
    the tunnel agree about the proxy, with one deliberate exception: an IP
    literal entry that carries a port stays exact, where httpx suffix-matches.
    ``*`` disables proxying; a plain name matches itself and its subdomains;
    a leading dot matches subdomains only; a ``*``-prefixed entry matches
    nothing; an IP literal or ``localhost`` matches that exact text;
    ``host:port`` also requires the port; a URL-form entry such as
    ``https://example.com`` or ``all://*.example.com`` matches by scheme,
    host pattern and port like an httpx mount.

    :param host: Target hostname (no brackets), lowercase or not.
    :param port: Target port when explicit and not the scheme default, else
        None (httpx drops default ports before matching).
    :param no_proxy: Raw ``no_proxy`` value.
    :param http_scheme: ``"http"`` or ``"https"``, the HTTP scheme the tunnel
        scheme corresponds to.
    :param proxy_from_all: Whether the proxy came from the ``all_proxy``
        fallback; httpx then mounts it as ``all://``, which a scheme-specific
        wildcard bypass outranks.
    :returns: True when the target must be dialed directly.
    """
    host = host.lower()
    for raw_entry in no_proxy.split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if "://" in entry:
            if _matches_url_pattern(host, port, http_scheme, entry, proxy_from_all):
                return True
            continue
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
        if not entry_host or (entry_port is not None and entry_port != port):
            continue
        if entry_host.startswith("*"):
            # httpx mounts "*example.com" as a pattern no real host matches.
            continue
        if entry_host.startswith("."):
            if host.endswith(entry_host):
                return True
            continue
        if _ip_literal(entry_host) is not None or entry_host == "localhost":
            if host == entry_host:
                return True
            continue
        if host == entry_host or host.endswith("." + entry_host):
            return True
    return False


def _matches_url_pattern(
    host: str, port: int | None, http_scheme: str, pattern: str, proxy_from_all: bool
) -> bool:
    """Match a URL-form ``no_proxy`` entry the way an httpx mount pattern would.

    :param host: Lowercased target hostname.
    :param port: Explicit non-default target port, else None.
    :param http_scheme: HTTP scheme corresponding to the tunnel scheme.
    :param pattern: Lowercased entry containing ``://``.
    :param proxy_from_all: Whether the proxy came from ``all_proxy``.
    :returns: True when the entry covers the target.
    """
    try:
        parts = urlsplit(pattern)
        pattern_port = parts.port
    except ValueError:
        return False
    pattern_host = parts.hostname or ""
    if parts.scheme not in ("all", http_scheme) or not pattern_host:
        return False
    if pattern_port == _DEFAULT_PORT_BY_HTTP_SCHEME.get(parts.scheme):
        pattern_port = None
    if pattern_port is not None and pattern_port != port:
        return False
    if pattern_host == "*":
        # A bare wildcard only outranks httpx's proxy mount when it is
        # port-qualified, or when it names the scheme and the proxy itself is
        # the scheme-less ``all://`` mount from all_proxy.
        return pattern_port is not None or (proxy_from_all and parts.scheme != "all")
    if pattern_host.startswith("*."):
        return host.endswith(pattern_host[1:])
    if pattern_host.startswith("*"):
        return host == pattern_host[1:] or host.endswith("." + pattern_host[1:])
    return host == pattern_host


def _ip_literal(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse *text* as an IP address, or return None for a hostname.

    :param text: Candidate address or hostname.
    :returns: The parsed address, or None.
    """
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def ws_env_proxy_url(ws_url: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Return the CONNECT proxy URL the environment mandates for *ws_url*.

    Mirrors the standard env semantics the host's own HTTP calls already
    honor: ``ws://`` follows ``http_proxy``, ``wss://`` follows
    ``https_proxy``, both fall back to ``all_proxy``, and ``no_proxy``
    bypasses matching hosts. Loopback targets always dial direct, matching
    the ``trust_env`` guard on the server-bound HTTP clients.

    :param ws_url: Tunnel URL, e.g. ``"wss://server/v1/hosts/h/tunnel"``.
    :param environ: Environment mapping (defaults to ``os.environ``).
    :returns: The proxy URL to CONNECT through, or None to dial direct
        (no proxy configured, loopback or bypassed target, or unsupported
        scheme).
    """
    if environ is None:
        environ = os.environ
    try:
        parts = urlsplit(ws_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    proxy_env = _PROXY_ENV_BY_WS_SCHEME.get(scheme)
    if proxy_env is None or not host:
        return None
    # A proxy resolves loopback against itself and can never reach this
    # machine's local server, so a local host must keep dialing direct.
    if is_loopback_url(ws_url):
        return None
    proxy = _env(environ, proxy_env)
    if proxy is None:
        # Remember which variable supplied the value so warnings name it and
        # the bypass rules can account for httpx's scheme-less all:// mount.
        proxy_env = "all_proxy"
        proxy = _env(environ, proxy_env)
    if not proxy:
        return None
    no_proxy = _env(environ, "no_proxy")
    explicit_port = None if port == _DEFAULT_PORT_BY_WS_SCHEME[scheme] else port
    if no_proxy and _bypassed_by_no_proxy(
        host,
        explicit_port,
        no_proxy,
        _HTTP_SCHEME_BY_WS_SCHEME[scheme],
        proxy_from_all=proxy_env == "all_proxy",
    ):
        return None
    if "://" not in proxy:
        # Bare host:port proxy values are conventionally plain HTTP.
        proxy = f"http://{proxy}"
    try:
        proxy_scheme = (urlsplit(proxy).scheme or "").lower()
    except ValueError:
        _warn_once(
            (proxy_env, "malformed"),
            "Ignoring malformed %s value for WebSocket tunnels; dialing direct",
            proxy_env,
        )
        return None
    if proxy_scheme not in _SUPPORTED_PROXY_SCHEMES:
        _warn_once(
            (proxy_env, proxy_scheme),
            "Ignoring %s proxy scheme %r for WebSocket tunnels: only http:// "
            "CONNECT proxies are supported; dialing direct",
            proxy_env,
            proxy_scheme,
        )
        return None
    return proxy


def redact_proxy_url(proxy_url: str) -> str:
    """Return *proxy_url* reduced to ``scheme://host[:port]`` for logging.

    Userinfo credentials and any path, query, or fragment (unused by the
    dialer, and a place for stray secrets) are dropped.

    :param proxy_url: Proxy URL, possibly carrying ``user:pass@``.
    :returns: The URL's scheme and authority only.
    """
    parts = urlsplit(proxy_url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


async def open_proxy_connect_socket(
    proxy_url: str, ws_url: str, *, timeout: float
) -> socket.socket:
    """Establish a CONNECT tunnel to *ws_url*'s origin through *proxy_url*.

    The proxy resolves the target's name itself, so this works where the
    dialing process has no direct DNS or TCP path. TLS for ``wss://`` is
    layered on top of the returned socket by the caller's ``connect()``.

    :param proxy_url: HTTP proxy URL from :func:`ws_env_proxy_url`.
    :param ws_url: Tunnel URL whose origin the proxy should reach.
    :param timeout: Overall budget, in seconds, for the dial and CONNECT handshake.
    :returns: The connected socket, ready for ``websockets``' ``sock=``.
    :raises OSError: When either URL is unusable, the proxy is unreachable,
        refuses the CONNECT, answers with something other than HTTP, or
        exhausts the budget.
    """
    try:
        proxy = urlsplit(proxy_url)
        target = urlsplit(ws_url)
    except ValueError as exc:
        raise OSError(f"malformed proxy or tunnel URL: {exc}") from exc
    if not proxy.hostname:
        raise OSError(f"proxy URL has no host: {redact_proxy_url(proxy_url)!r}")
    if not target.hostname:
        raise OSError(f"tunnel URL has no host: {ws_url!r}")
    target_host = target.hostname
    if _ip_literal(target_host) is None:
        try:
            # The CONNECT authority is ASCII; encode an IDN host the way the
            # direct-dial resolver would.
            target_host = target_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise OSError(f"tunnel host is not IDNA-encodable: {target_host!r}") from exc
    try:
        proxy_port = proxy.port or 80
        target_port = target.port or _DEFAULT_PORT_BY_WS_SCHEME.get(
            (target.scheme or "").lower(), 80
        )
    except ValueError as exc:
        # urlsplit only rejects a non-numeric port when it is read.
        raise OSError(f"invalid port in proxy or tunnel URL: {exc}") from exc
    auth_header: str | None = None
    if proxy.username is not None:
        credentials = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}"
        auth_header = "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    dial = asyncio.ensure_future(
        asyncio.to_thread(
            _connect_sync,
            proxy.hostname,
            proxy_port,
            target_host,
            target_port,
            auth_header,
            timeout,
        )
    )
    try:
        # One overall deadline covers resolution, every address attempt and
        # the handshake; the worker's own per-operation budget is secondary.
        return await asyncio.wait_for(asyncio.shield(dial), timeout)
    except asyncio.CancelledError:
        # The worker thread cannot be interrupted; close any socket it still
        # hands back after this cancellation instead of orphaning it.
        dial.add_done_callback(_close_dial_result)
        raise
    except TimeoutError:
        # Registered first: the worker may finish just as the deadline fires.
        dial.add_done_callback(_close_dial_result)
        if dial.done() and not dial.cancelled() and dial.exception() is not None:
            raise  # the worker's own budget fired; keep its message
        raise TimeoutError(
            f"proxy did not complete CONNECT to {target_host}:{target_port} within {timeout:g}s"
        ) from None


def _close_dial_result(dial: asyncio.Future[socket.socket]) -> None:
    """Close the socket a cancelled CONNECT dial still produced.

    :param dial: The finished dial future.
    """
    if not dial.cancelled() and dial.exception() is None:
        with contextlib.suppress(OSError):
            dial.result().close()


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
    :param timeout: Budget, in seconds, for this worker's dial and handshake;
        the dial applies it per resolved proxy address, and the awaiting
        caller enforces the overall deadline.
    :returns: The connected socket with its timeout cleared.
    :raises OSError: On dial failure, a refused/garbled CONNECT, or an
        exhausted budget.
    """
    authority = f"[{target_host}]" if ":" in target_host else target_host
    authority = f"{authority}:{target_port}"
    deadline = time.monotonic() + timeout

    def remaining_budget() -> float:
        # One budget for the whole handshake, so a slow dial or a proxy that
        # drips bytes cannot stretch it across many per-operation timeouts.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"proxy did not complete CONNECT to {authority} within {timeout:g}s"
            )
        return remaining

    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        request_lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
        if auth_header is not None:
            request_lines.append(f"Proxy-Authorization: {auth_header}")
        sock.settimeout(remaining_budget())
        sock.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1"))
        response = b""
        while b"\r\n\r\n" not in response:
            sock.settimeout(remaining_budget())
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError(f"proxy closed the connection during CONNECT to {authority}")
            response += chunk
            if len(response) > _MAX_CONNECT_RESPONSE_BYTES:
                raise OSError(f"proxy sent an oversized response to CONNECT {authority}")
        head, _, residue = response.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        status_parts = status_line.split(" ", 2)
        if len(status_parts) < 2 or not status_parts[0].startswith("HTTP/"):
            raise OSError(
                f"proxy sent a non-HTTP response to CONNECT {authority}: {status_line!r}"
            )
        status_text = status_parts[1]
        status_code = int(status_text) if status_text.isascii() and status_text.isdigit() else 0
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
