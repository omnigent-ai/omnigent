"""Safety checks for opt-in e2e-ui external server reuse."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

DEV_PORTS = frozenset({6767, 8000, 5173})
# Literal hostnames are a fast path; resolved IPs are classified by _dev_address_reason.
DEV_HOSTNAMES = frozenset({"localhost", "0.0.0.0"})


def unsafe_ui_base_url_reason(base_url: str) -> str | None:
    """Return why ``base_url`` looks unsafe for shared e2e-ui reuse.

    ``--ui-base-url`` points tests at a server this pytest session did not
    spawn. Known dev ports and local/private hosts are refused by default so a
    run cannot silently share a developer server, database, or artifact store.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "it must be an absolute http(s) URL with a host"

    host = parsed.hostname.rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError:
        return "it has an invalid port"
    # No explicit port is not itself unsafe; host checks below still apply.
    if port is not None and port in DEV_PORTS:
        return f"port {port} is a known Omnigent/Vite dev port"
    if host in DEV_HOSTNAMES:
        return f"host {host!r} is a local dev host"

    host_reason = _dev_host_reason(host)
    if host_reason is not None:
        return host_reason
    return None


def _dev_host_reason(host: str) -> str | None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return _resolved_dev_host_reason(host)
    return _dev_address_reason(address)


def _resolved_dev_host_reason(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    for info in infos:
        address_text = info[4][0]
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            continue
        reason = _dev_address_reason(address)
        if reason is not None:
            return f"host {host!r} resolves to {address_text}, {reason}"
    return None


def _dev_address_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if address.is_loopback:
        return "a loopback address"
    if address.is_unspecified:
        return "an unspecified local address"
    if address.is_link_local:
        return "a link-local address"
    if address.is_private:
        return "a private-network address"
    return None
