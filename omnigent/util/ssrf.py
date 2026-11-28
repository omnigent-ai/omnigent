"""
Shared SSRF host classification.

:func:`host_is_internal` decides whether a URL host is, or resolves to, a
non-globally-reachable address. It is used both at MCP-server *registration*
time (rejecting an internal ``url`` before it is persisted) and at *connect*
time (rejecting an external→internal HTTP redirect), so the two enforcement
points share one definition of "internal".
"""

from __future__ import annotations

import ipaddress
import socket

# IPv6 transition prefixes that embed an IPv4 destination in their low 32 bits.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPAT_PREFIX = ipaddress.ip_network("::/96")


def _non_public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # `is_global` is the direct "globally reachable" test; its negation covers
    # loopback, private (RFC1918 / ULA), link-local (incl. the 169.254.169.254
    # cloud-metadata address), shared / CGNAT (100.64.0.0/10 — which contains
    # Alibaba's 100.100.100.200 metadata endpoint), reserved and unspecified —
    # without enumerating each range and missing one (shared space, for
    # instance, is neither `is_private` nor `is_reserved`). `is_multicast` is
    # OR'd in defensively since some Python versions have reported certain
    # multicast addresses as global.
    return not addr.is_global or addr.is_multicast


def _blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # An IPv6 literal can carry an internal IPv4 destination whose address the
    # outer IPv6 flags do not reflect (6to4 ``2002:a9fe:a9fe::`` / NAT64
    # ``64:ff9b::a9fe:a9fe`` / IPv4-mapped / IPv4-compatible for
    # 169.254.169.254). Decode any embedded IPv4 and judge it too, so these
    # transition forms can't smuggle a metadata/internal target past the gate.
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [addr]
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped or addr.sixtofour
        if embedded is not None:
            candidates.append(embedded)
        low32 = int(addr) & 0xFFFFFFFF
        # NAT64 well-known prefix and the deprecated IPv4-compatible ::/96.
        if addr in _NAT64_PREFIX or (addr in _V4_COMPAT_PREFIX and low32 > 1):
            candidates.append(ipaddress.IPv4Address(low32))
    return any(_non_public(candidate) for candidate in candidates)


def host_is_internal(host: str) -> bool:
    """
    Whether *host* is (or resolves to) a non-globally-reachable address.

    Blocks loopback, private (RFC1918 / ULA), link-local (incl. the
    169.254.169.254 cloud-metadata address), shared / CGNAT (100.64.0.0/10),
    reserved, multicast and unspecified targets. An IP literal is judged
    directly; a hostname is resolved and every returned address is judged, so a
    name that points at an internal address is blocked too. Best-effort: it
    performs blocking DNS resolution (call it off the event loop), and it does
    not by itself defeat DNS rebinding — the address can change between this
    check and the transport's own connect.

    :param host: The URL host (hostname or IP literal).
    :returns: ``True`` when the host is, or resolves to, a non-public address.
    """
    try:
        return _blocked(ipaddress.ip_address(host))
    except ValueError:
        pass  # not a literal — resolve the name below
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        # Unresolvable: fail closed — a host that does not resolve here is not a
        # valid public endpoint.
        return True
    resolved = {info[4][0] for info in infos}
    if not resolved:
        return True
    return any(_blocked(ipaddress.ip_address(ip)) for ip in resolved)
