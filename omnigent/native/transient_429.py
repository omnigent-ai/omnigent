"""Bounded retry of transient HTTP 429 throttles on native request paths.

The Omnigent server or its ingress can transiently rate-limit a request
(HTTP 429, e.g. ``RESOURCE_EXHAUSTED``). Native startup and policy
requests treat an explicit 429 as retryable within a bounded budget,
honouring a bounded ``Retry-After`` hint. Every other 4xx stays final,
and transport errors keep the caller's existing handling.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

# Total time spent retrying a throttled request before the caller's
# normal failure handling takes over. Mirrors the policy hook's
# transient retry budget.
TRANSIENT_429_RETRY_BUDGET_S = 30.0
TRANSIENT_429_INITIAL_BACKOFF_S = 1.0
TRANSIENT_429_MAX_BACKOFF_S = 10.0

# Indirections so tests can stub waiting/clocking without clobbering the
# shared ``asyncio``/``time`` modules (see dev/lint/lint_no_global_asyncio_patch.py).
_monotonic = time.monotonic
_sleep = asyncio.sleep


def retry_after_hint_s(resp: httpx.Response) -> float | None:
    """
    Parse a usable ``Retry-After`` hint from a throttled response.

    :param resp: The HTTP 429 response.
    :returns: The requested wait in seconds, capped at
        :data:`TRANSIENT_429_MAX_BACKOFF_S`; ``None`` when the header is
        absent, negative, or in the HTTP-date form (normal backoff then
        applies).
    """
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, TRANSIENT_429_MAX_BACKOFF_S)


async def send_with_transient_429_retry(
    send: Callable[[], Awaitable[httpx.Response]],
) -> httpx.Response:
    """
    Send a request, retrying explicit 429 responses with bounded backoff.

    :param send: Zero-argument coroutine factory performing one attempt,
        e.g. ``lambda: client.post(url, json=body)``. Transport errors it
        raises propagate unchanged from any attempt.
    :returns: The first non-429 response, or the last 429 once
        :data:`TRANSIENT_429_RETRY_BUDGET_S` is spent — the caller's
        existing ``status >= 400`` handling then fires.
    """
    deadline = _monotonic() + TRANSIENT_429_RETRY_BUDGET_S
    backoff_s = TRANSIENT_429_INITIAL_BACKOFF_S
    while True:
        resp = await send()
        if resp.status_code != httpx.codes.TOO_MANY_REQUESTS:
            return resp
        delay_s = max(backoff_s, retry_after_hint_s(resp) or 0.0)
        if _monotonic() + delay_s >= deadline:
            return resp
        await _sleep(delay_s)
        backoff_s = min(backoff_s * 2, TRANSIENT_429_MAX_BACKOFF_S)
