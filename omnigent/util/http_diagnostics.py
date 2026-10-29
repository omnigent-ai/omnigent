"""Bounded transport failure metadata without request or exception content."""

from __future__ import annotations

import httpx


def http_failure_attributes(error: BaseException) -> dict[str, object]:
    """Preserve transport phase and nested OS causes through HTTP wrappers."""
    phase = "unknown"
    for exception_types, name in (
        ((httpx.ConnectError, httpx.ConnectTimeout), "connect"),
        ((httpx.ReadError, httpx.ReadTimeout), "read"),
        ((httpx.WriteError, httpx.WriteTimeout), "write"),
        ((httpx.PoolTimeout,), "pool"),
        ((httpx.ProtocolError,), "protocol"),
        ((httpx.HTTPStatusError,), "response_status"),
    ):
        if isinstance(error, exception_types):
            phase = name
            break

    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    errno: int | None = None
    suppressed_context = False
    while current is not None and len(chain) < 5 and id(current) not in seen:
        seen.add(id(current))
        chain.append(type(current).__name__[:80])
        if isinstance(current, OSError) and type(current.errno) is int:
            errno = current.errno
        if current.__cause__ is not None:
            current = current.__cause__
        else:
            suppressed_context |= current.__suppress_context__ and current.__context__ is not None
            current = current.__context__
    return {
        "transport_phase": phase,
        "exception_cause_chain": "/".join(chain),
        "transport_errno": errno,
        "exception_chain_truncated": current is not None,
        "exception_suppressed_context": suppressed_context,
    }
