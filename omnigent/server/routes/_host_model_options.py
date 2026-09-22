"""One round-trip helper for ``host.model_options``.

Two callers ask a host which models a harness could launch with: the
``/v1/hosts/{id}/model-options`` route (which turns a failure into an HTTP
error) and the session-create routing path (which degrades to no
candidates). Only the failure handling differs, so the request-id /
future / frame / timeout / cleanup shape lives here once.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from omnigent.host.frames import HostModelOptionsFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry

# Repeats within this window are served from the connection's cache instead of
# re-probing the host. Matches the composer's client-side `staleTime`, so its
# own polling still catches a provider change within one interval.
_MODEL_OPTIONS_CACHE_TTL_S = 15.0


async def request_host_model_options(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    harness: str,
    timeout_s: float,
) -> dict[str, Any]:
    """
    Send a ``host.model_options`` frame and await the host's result.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection to query.
    :param harness: Native harness id, e.g. ``"claude-native"``.
    :param timeout_s: Seconds to wait for the result frame.
    :returns: The result payload, e.g. ``{"status": "ok", "models": [...]}``.
    :raises ConnectionError: The host connection dropped before the frame
        could be enqueued.
    :raises asyncio.TimeoutError: The host did not answer within
        *timeout_s*.
    """
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_model_options[request_id] = future
    frame = encode_host_frame(HostModelOptionsFrame(request_id=request_id, harness=harness))
    try:
        host_registry.send_text(host_conn, frame)
        return await asyncio.wait_for(future, timeout=timeout_s)
    finally:
        host_conn.pending_model_options.pop(request_id, None)


async def cached_host_model_options(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    harness: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Model options for *harness*, served from a short per-connection cache.

    A live cache entry is returned without touching the host. Otherwise
    concurrent callers for the same harness share one in-flight probe
    (single-flight), so the composer's several observers cost one round-trip.
    Only ``status == "ok"`` results are cached; errors and timeouts always
    re-probe. Cache and in-flight state live on *host_conn*, so a reconnect
    starts clean.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection to query.
    :param harness: Native harness id, e.g. ``"claude-native"``.
    :param timeout_s: Seconds to wait for the result frame.
    :returns: The result payload, e.g. ``{"status": "ok", "models": [...]}``.
    """
    cached = host_conn.model_options_cache.get(harness)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    inflight = host_conn.inflight_model_options.get(harness)
    if inflight is None:

        async def _probe() -> dict[str, Any]:
            result = await request_host_model_options(
                host_registry=host_registry,
                host_conn=host_conn,
                harness=harness,
                timeout_s=timeout_s,
            )
            if isinstance(result, dict) and result.get("status") == "ok":
                host_conn.model_options_cache[harness] = (
                    time.monotonic() + _MODEL_OPTIONS_CACHE_TTL_S,
                    result,
                )
            return result

        inflight = asyncio.ensure_future(_probe())
        host_conn.inflight_model_options[harness] = inflight
        try:
            return await inflight
        finally:
            host_conn.inflight_model_options.pop(harness, None)
    # A probe for this harness is already running: await its shared result.
    return await asyncio.shield(inflight)
