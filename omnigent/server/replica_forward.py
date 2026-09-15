"""
Cross-replica forwarding for mis-routed session requests.

On a multi-replica deployment, a session's runner tunnel lives on the
replica its host connected to, but the ingress can route the session's
later requests elsewhere (the routing key's hash drifts across a pod
rollout, or the request carries no key). The replica without the tunnel
used to answer every such request with ``400 wrong_replica`` and rely on
the client's single keyless re-address — which only reaches the ingress
default replica, so a session whose tunnel lives anywhere else strands.

This module lets the mis-routed replica heal the request itself: each
replica advertises a directly reachable base URL, the tunnel-owning
replica stamps that URL on the host's DB row (``hosts.replica_url``),
and a replica that receives a request it cannot serve proxies it to the
owner and relays the response. The forwarded hop carries
:data:`REPLICA_FORWARDED_HEADER` so a stale row can never create a
forwarding loop — the second hop answers ``wrong_replica`` as before.

The forward target comes exclusively from ``hosts.replica_url``, written
only by servers at tunnel connect/heartbeat — never from client input —
so a client cannot steer the proxy at an arbitrary URL.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import AsyncIterator
from typing import Final

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

_logger = logging.getLogger(__name__)

#: Marks a request that already crossed one replica-to-replica hop.
#: The receiving replica never forwards such a request again.
REPLICA_FORWARDED_HEADER: Final[str] = "X-Omnigent-Replica-Forwarded"

#: Operator override for the advertised base URL, e.g. when replicas
#: reach each other through a service mesh name rather than a pod IP.
REPLICA_ADVERTISE_URL_ENV: Final[str] = "OMNIGENT_REPLICA_ADVERTISE_URL"

# RFC 9110 connection-scoped headers, never relayed across the hop.
_HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Recomputed for the relayed request/response rather than copied:
# ``host`` names the wrong server, ``content-length`` is re-derived from
# the relayed body, and ``accept-encoding`` is dropped so the internal
# hop stays identity-coded (httpx would transparently decode anyway).
_REQUEST_HEADERS_NOT_RELAYED: Final[frozenset[str]] = _HOP_BY_HOP_HEADERS | {
    "host",
    "content-length",
    "accept-encoding",
}
_RESPONSE_HEADERS_NOT_RELAYED: Final[frozenset[str]] = _HOP_BY_HOP_HEADERS | {
    "content-length",
    "content-encoding",
}

_FORWARD_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=10.0, read=120.0, write=30.0, pool=10.0
)
# Streams (SSE) idle between events for arbitrarily long; the browser or
# the upstream heartbeat, not a read timeout, decides when they end.
_STREAM_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=10.0, read=None, write=30.0, pool=10.0
)


def _primary_local_ip() -> str | None:
    """
    Return the IP the OS routes outbound traffic from, or ``None``.

    Uses the routing table via a connected UDP socket — no packet is
    sent. This is the address peers on the same network can dial, which
    a wildcard bind (``0.0.0.0``) does not reveal by itself.

    :returns: A dotted-quad IP, e.g. ``"10.68.3.7"``, or ``None`` when
        the machine has no route (e.g. no network at all).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.254.254.254", 1))
            return str(probe.getsockname()[0])
    except OSError:
        return None


def derive_replica_advertise_url(bind_host: str, port: int) -> str | None:
    """
    Compute the base URL peer replicas can reach this server on.

    Resolution order: the :data:`REPLICA_ADVERTISE_URL_ENV` operator
    override; a concrete bind host verbatim (including loopback — a
    single-replica local server never forwards, and the equality check
    against the row's URL keeps a stale self-pointing row from looping);
    for a wildcard bind, the machine's primary IP.

    :param bind_host: The ``--host`` the server binds, e.g. ``"0.0.0.0"``.
    :param port: The bound port, e.g. ``8000``.
    :returns: A base URL like ``"http://10.68.3.7:8000"``, or ``None``
        when no reachable address can be determined (forwarding then
        stays disabled and mis-routed requests fail exactly as before).
    """
    override = os.environ.get(REPLICA_ADVERTISE_URL_ENV, "").strip()
    if override:
        return override.rstrip("/")
    host = bind_host.strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        derived = _primary_local_ip()
        if derived is None:
            return None
        host = derived
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _build_async_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Build the forwarding client. Seam for in-process test transports.

    :param timeout: The timeout profile for this hop.
    :returns: A fresh :class:`httpx.AsyncClient`.
    """
    return httpx.AsyncClient(timeout=timeout)


def _relay_request_headers(request: Request) -> dict[str, str]:
    """Copy the inbound headers the cross-replica hop should carry.

    Auth material (bearer tokens, cookies) passes through untouched so
    the owning replica authenticates the original caller itself.

    :param request: The mis-routed inbound request.
    :returns: Header map for the outbound hop, guard header included.
    """
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _REQUEST_HEADERS_NOT_RELAYED
    }
    headers[REPLICA_FORWARDED_HEADER] = "1"
    return headers


def _relay_response_headers(upstream: httpx.Response) -> dict[str, str]:
    """Copy the upstream headers worth relaying to the original caller.

    :param upstream: The owning replica's response.
    :returns: Header map for the relayed response.
    """
    return {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() not in _RESPONSE_HEADERS_NOT_RELAYED
    }


async def forward_misrouted_request(
    request: Request,
    target_base_url: str,
    *,
    stream: bool = False,
) -> Response | None:
    """
    Proxy *request* to the replica at *target_base_url* and relay the reply.

    The upstream response — success or error — is relayed verbatim, so
    the caller behaves exactly as if the ingress had routed correctly in
    the first place. Only a transport failure (the advertised URL is
    unreachable, e.g. the owning replica just died) returns ``None``,
    letting the caller fall back to the pre-existing ``wrong_replica``
    error and the client-side re-address.

    :param request: The mis-routed inbound request. Its body must already
        be buffered (FastAPI has consumed it by the time routes run).
    :param target_base_url: The owning replica's advertised base URL,
        from ``hosts.replica_url`` — never from client input.
    :param stream: When ``True``, relay the upstream body incrementally
        (for SSE); otherwise buffer the complete response.
    :returns: The relayed response, or ``None`` on transport failure.
    """
    url = target_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = _relay_request_headers(request)
    body = await request.body()

    if not stream:
        try:
            async with _build_async_client(_FORWARD_TIMEOUT) as client:
                upstream = await client.request(request.method, url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            _logger.warning("Cross-replica forward to %s failed: %s", target_base_url, exc)
            return None
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=_relay_response_headers(upstream),
        )

    client = _build_async_client(_STREAM_TIMEOUT)
    try:
        upstream = await client.send(
            client.build_request(request.method, url, content=body, headers=headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        _logger.warning("Cross-replica forward to %s failed: %s", target_base_url, exc)
        return None

    async def _relay_body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _relay_body(),
        status_code=upstream.status_code,
        headers=_relay_response_headers(upstream),
    )
