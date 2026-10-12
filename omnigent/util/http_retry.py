"""Shared helpers for bounded HTTP retry delays."""

from __future__ import annotations

import math
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
    value = response.headers.get("retry-after")
    if value is None:
        return fallback
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return fallback
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at - datetime.now(UTC)).total_seconds()
    if not math.isfinite(delay) or delay <= 0:
        return fallback
    return min(delay, max_delay)
