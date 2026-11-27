"""Common POST outcomes for settings ownership and diagnostic-drain scenarios."""

from __future__ import annotations

import asyncio

import httpx

UNCERTAIN_POST_ERRORS = {
    "read": httpx.ReadError,
    "write": httpx.WriteError,
    "timeout": httpx.ReadTimeout,
    "cancel": asyncio.CancelledError,
}
UNDELIVERED_POST_ERRORS = {
    "connect": httpx.ConnectError,
    "connect-timeout": httpx.ConnectTimeout,
    "pool-timeout": httpx.PoolTimeout,
    "cleanup-failure": httpx.ConnectError,
}
POST_ERRORS = UNCERTAIN_POST_ERRORS | UNDELIVERED_POST_ERRORS
POST_RESPONSE_FAILURES = ("503", "no-ack", "mismatched")
V2_POST_FAILURES = (*POST_ERRORS, *POST_RESPONSE_FAILURES, "reactivated-ack")
