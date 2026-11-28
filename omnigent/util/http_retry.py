"""Shared helpers for bounded HTTP retry delays."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx


def bounded_retry_after_seconds(
    response: httpx.Response,
    *,
    fallback: float,
    max_delay: float,
) -> float:
    """Return a bounded ``Retry-After`` delay, or the fallback.

    A zero or past hint uses the fallback, so repeated hints cannot cause
    back-to-back retries.
    """
    delay = _retry_after_seconds(response)
    return fallback if delay is None else min(delay, max_delay)


def jittered_retry_delay(
    response: httpx.Response | None,
    *,
    backoff: float,
    max_delay: float,
) -> float | None:
    """Add jitter without retrying before a server's ``Retry-After`` hint.

    Return ``None`` if the hint exceeds the caller's wait limit, so the caller
    can stop retrying instead of shortening the server's requested delay.
    """
    retry_after = _retry_after_seconds(response) if response is not None else None
    delay = max(backoff, retry_after or 0.0)
    if delay > max_delay:
        return None
    return delay + random.uniform(0.0, min(backoff, max_delay - delay))


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at - datetime.now(UTC)).total_seconds()
    if not math.isfinite(delay) or delay <= 0:
        return None
    return delay
