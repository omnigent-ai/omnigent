"""Shared OAuth helpers for the token-granting route modules.

Every OAuth grant router answers failures with the same RFC-shaped body, sends
the same no-store headers on anything carrying a token, and throttles its
unauthenticated endpoints with the same limiter, so all three live here rather
than once per module::

    from omnigent.server.routes._oauth import NO_STORE_HEADERS, oauth_error

    return oauth_error("invalid_client", status_code=401)
    return JSONResponse(status_code=200, content=body, headers=NO_STORE_HEADERS)
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from starlette.responses import JSONResponse

# RFC 6749 §5.1: a response carrying tokens or credentials must not be cached.
# §5.2's error response carries the same pair, so every token-endpoint answer —
# success or failure — sends them. Read-only, because this one mapping is passed
# to responses from both grant routers: were it mutable, a caller that edited it
# would silently re-header every later token response in the process.
NO_STORE_HEADERS: Mapping[str, str] = MappingProxyType(
    {"Cache-Control": "no-store", "Pragma": "no-cache"}
)


def oauth_error(
    error: str,
    status_code: int = 400,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build an RFC 6749 / 8628 shaped OAuth error response.

    :param error: The OAuth error code, e.g. ``"invalid_client"``.
    :param status_code: HTTP status to answer with.
    :param headers: Extra headers merged over :data:`NO_STORE_HEADERS`, e.g.
        the ``WWW-Authenticate`` challenge RFC 6749 §5.2 requires on a 401
        that rejected an ``Authorization`` header.
    :returns: A ``JSONResponse`` carrying ``{"error": <error>}``.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers={**NO_STORE_HEADERS, **(headers or {})},
    )


# Hard cap on distinct keys a limiter tracks at once. Bounds memory even
# under a spray from many source IPs (e.g. a whole IPv6 /64) — without it a
# key hit once and never revisited would live forever. When the cap is hit
# the whole table is swept of aged-out keys; if still full, the limiter
# fails OPEN for a new key (availability over a soft throttle — the real
# anti-abuse control in production is the confidential client secret).
RATE_LIMITER_MAX_KEYS = 10_000


class SlidingWindowRateLimiter:
    """Minimal per-key sliding-window limiter (in-memory, single-process).

    Keyed by client IP. Adequate for a single-process deployment; a
    multi-replica server would want a shared store, but each grant's own
    semantics (single-use device codes, a capped token TTL) already bound
    abuse.

    Memory is bounded by :data:`RATE_LIMITER_MAX_KEYS`: keys are dropped
    when they age out (on touch) and, when the cap is reached, a full sweep
    reclaims every aged-out key before admitting a new one.
    """

    def __init__(self, max_events: int, window_seconds: int, max_keys: int) -> None:
        self._max = max_events
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: dict[str, list[float]] = {}

    def _sweep(self, cutoff: float) -> None:
        """Drop every key whose hits have all aged out."""
        dead = [k for k, ts in self._hits.items() if not any(t > cutoff for t in ts)]
        for k in dead:
            self._hits.pop(k, None)

    def allow(self, key: str, now: float) -> bool:
        cutoff = now - self._window
        # New key while at capacity: sweep aged-out keys first; if the table
        # is still full of live keys, fail open rather than grow unbounded.
        if key not in self._hits and len(self._hits) >= self._max_keys:
            self._sweep(cutoff)
            if len(self._hits) >= self._max_keys:
                return True
        hits = [t for t in self._hits.get(key, ()) if t > cutoff]
        # Opportunistically bound memory: drop keys that fully aged out.
        if not hits:
            self._hits.pop(key, None)
        if len(hits) >= self._max:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True
