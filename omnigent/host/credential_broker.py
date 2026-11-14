"""Fetch the session owner's per-user credential from the server's broker.

A managed sandbox host authenticates with its launch token and asks the
provider-generic broker (:mod:`omnigent.server.routes.host_credentials`) for
the owner's connected credential, e.g. ``databricks`` or ``claude``. Callers
interpret the provider-specific payload; this module owns only the request and
its retry budget.
"""

from __future__ import annotations

import time

import httpx

from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.telemetry.request_headers import client_identity_headers

_TIMEOUT_S = 15.0

# Pauses between broker attempts. A transient blip otherwise surfaces as a
# missing credential, which callers treat as "not connected".
_RETRY_BACKOFF_S: tuple[float, ...] = (0.5, 1.0)


def credential_url(server: str, host_id: str, provider: str) -> str:
    """Return the broker URL for *provider*'s credential on *host_id*."""
    return f"{server.rstrip('/')}/v1/hosts/{host_id}/credentials/{provider}"


def fetch_host_credential(
    server: str, host_id: str, host_token: str, provider: str
) -> dict | None:
    """Fetch *provider*'s broker JSON, or ``None`` on a permanent failure or exhausted retries.

    Connection errors, 429 and 5xx are retried; any other non-200 is final. All
    attempts share one :data:`_TIMEOUT_S` budget, so retrying never outlasts the
    single attempt a caller's helper timeout was sized for.
    """
    deadline = time.monotonic() + _TIMEOUT_S
    for attempt in range(len(_RETRY_BACKOFF_S) + 1):
        if attempt:
            pause = _RETRY_BACKOFF_S[attempt - 1]
            if time.monotonic() + pause >= deadline:
                return None
            time.sleep(pause)
        try:
            resp = httpx.get(
                credential_url(server, host_id, provider),
                headers={**client_identity_headers(), MANAGED_HOST_TOKEN_HEADER: host_token},
                timeout=max(deadline - time.monotonic(), 0.1),
            )
        except httpx.TransportError:
            continue
        except httpx.HTTPError:
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            continue
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None
