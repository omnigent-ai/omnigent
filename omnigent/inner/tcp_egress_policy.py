"""Validate named, fixed-destination TCP grants."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence

from omnigent.errors import OmnigentError

from .datamodel import TcpEgressRoute
from .egress.proxy import _CLOUD_TRAP_NETWORKS

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DNS = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_BLOCKED_ZERO_NETWORK = ipaddress.ip_network("0.0.0.0/8")
_BLOCKED_CONTROL = ipaddress.ip_network("100.100.100.200/32")
_BLOCKED_IPV6_METADATA = (
    ipaddress.ip_network("fd00:ec2::/32"),
    ipaddress.ip_network("fd20:ce::254/128"),
)


def is_blocked_tcp_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Exclude local, reserved, and cloud-control destinations even with a CIDR."""
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or address in _BLOCKED_ZERO_NETWORK
        or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None)
        or any(
            address in network
            for network in (*_CLOUD_TRAP_NETWORKS, _BLOCKED_CONTROL, *_BLOCKED_IPV6_METADATA)
        )
    )


def parse_tcp_egress(
    raw: object, *, egress_rules: Sequence[str] = ()
) -> list[TcpEgressRoute] | None:
    """Reject broad, ambiguous, or HTTP-overlapping opaque TCP routes."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise OmnigentError("os_env.sandbox.tcp_egress must be a non-empty list")
    if egress_rules:
        raise OmnigentError(
            "os_env.sandbox.tcp_egress cannot coexist with HTTP egress_rules: "
            "an opaque route could bypass HTTP method and path restrictions via a DNS alias"
        )
    routes: list[TcpEgressRoute] = []
    names: set[str] = set()
    for index, entry in enumerate(raw):
        label = f"os_env.sandbox.tcp_egress[{index}]"
        if not isinstance(entry, dict) or set(entry) - {"name", "host", "port", "allowed_cidrs"}:
            raise OmnigentError(f"{label} must be a mapping with supported fields only")
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            raise OmnigentError(f"{label}.name must be unique and ASCII lowercase")
        host = entry.get("host")
        if not isinstance(host, str):
            raise OmnigentError(f"{label}.host must be an exact ASCII DNS name or IP address")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            if not _DNS.fullmatch(host):
                raise OmnigentError(
                    f"{label}.host must be an exact ASCII DNS name or IP address"
                ) from None
        if address is not None and is_blocked_tcp_address(address):
            raise OmnigentError(f"{label}.host is a blocked address")
        host = str(address) if address is not None else host.lower()
        port = entry.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise OmnigentError(f"{label}.port must be an integer from 1 to 65535")
        if port in (80, 443):
            raise OmnigentError(f"{label} overlaps HTTP egress or a standard HTTP port")
        raw_cidrs = entry.get("allowed_cidrs", [])
        if not isinstance(raw_cidrs, list) or any(not isinstance(v, str) for v in raw_cidrs):
            raise OmnigentError(f"{label}.allowed_cidrs must be a list of CIDRs")
        networks: list[str] = []
        for value in raw_cidrs:
            try:
                network = ipaddress.ip_network(value, strict=True)
            except ValueError as exc:
                raise OmnigentError(f"{label}.allowed_cidrs contains invalid CIDR") from exc
            if (
                network.prefixlen == 0
                or network.is_loopback
                or network.is_link_local
                or network.is_multicast
                or network.is_unspecified
                or network.is_reserved
                or network.overlaps(_BLOCKED_ZERO_NETWORK)
                or any(
                    network.version == blocked.version
                    and network.prefixlen >= blocked.prefixlen
                    and (int(network.network_address) & int(blocked.netmask))
                    == int(blocked.network_address)
                    for blocked in (
                        *_CLOUD_TRAP_NETWORKS,
                        _BLOCKED_CONTROL,
                        *_BLOCKED_IPV6_METADATA,
                    )
                )
            ):
                raise OmnigentError(
                    f"{label}.allowed_cidrs includes a blocked or unrestricted network"
                )
            networks.append(str(network))
        if address is not None:
            if networks:
                allowed = any(address in ipaddress.ip_network(value) for value in networks)
            else:
                allowed = address.is_global
            if not allowed:
                raise OmnigentError(f"{label}.host is outside its allowed addresses")
        names.add(name)
        routes.append(TcpEgressRoute(name, host, port, tuple(networks)))
    return routes


def normalize_tcp_egress(
    routes: Sequence[TcpEgressRoute], *, egress_rules: Sequence[str] = ()
) -> list[TcpEgressRoute]:
    """Validate routes supplied by code as strictly as those supplied by YAML."""
    if not routes:
        raise ValueError("TCP egress requires at least one route")
    result = parse_tcp_egress(
        [
            {
                "name": route.name,
                "host": route.host,
                "port": route.port,
                "allowed_cidrs": list(route.allowed_cidrs),
            }
            for route in routes
        ],
        egress_rules=egress_rules,
    )
    if result is None:
        raise ValueError("TCP egress requires at least one route")
    return result
