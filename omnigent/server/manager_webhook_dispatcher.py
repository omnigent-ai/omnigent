"""Manager-webhook delivery dispatcher (OMN-104).

A single ``asyncio.create_task`` running inside the FastAPI lifespan (the
same pattern as ``metrics_publish_task`` — see ``omnigent/server/app.py``),
one per server replica, safe under multi-replica because claiming is
row-leased (``FOR UPDATE SKIP LOCKED`` on PostgreSQL; a plain claim under
SQLite's single-writer serialization), not because only one replica runs
it.

HTTP delivery is fully decoupled from the DB transaction and from
``_publish_status``/SSE/conversation writes: the outbox INSERT (see
``omnigent/server/session_outbox.py``) is synchronous with the producing
transaction, but this loop is a separate task with no code path that writes
back into session state — a slow or unreachable manager endpoint never
delays a session's terminal transitions.

The task always starts; when ``manager_webhook.enabled`` is ``False`` (the
default) it still runs its claim/reclaim loop but the claim query returns
nothing meaningful to deliver in practice (see :func:`_run_once`) — this
means flipping the config live needs no server restart. Poll/reconciliation
is untouched by any of this: nothing here can regress existing session-state
correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from contextlib import suppress

import httpx

from omnigent.entities import LifecycleOutboxEvent
from omnigent.runtime.telemetry import _REDACT_KEY_SUBSTRINGS
from omnigent.server import manager_webhook_signing as signing
from omnigent.server.server_config import ManagerWebhookConfigError, manager_webhook_config
from omnigent.stores.session_lifecycle_store import SessionLifecycleStore

_logger = logging.getLogger(__name__)

# Poll cadence when idle (nothing claimed). Short enough that an enabled
# webhook feels responsive; cheap enough (one indexed SELECT) to run
# constantly even while disabled.
_IDLE_POLL_SECONDS = 2.0
# How many rows to claim per cycle.
_CLAIM_BATCH_SIZE = 25
# How long a claim's lease is held before it's reclaimable (crash-mid-claim
# recovery). Comfortably longer than any single delivery attempt's timeout.
_LEASE_SECONDS = 60
_DELIVERY_TIMEOUT_SECONDS = 10.0
# Backoff schedule (docs/architecture/2026-08-10-durable-session-lifecycle-push.md §7.3):
# exponential with full jitter, capped, unbounded attempt count.
_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_CAP_SECONDS = 15 * 60.0
# After this many attempts a row is marked dead_letter for API/log
# visibility — it keeps retrying at the capped floor interval regardless
# (escalation, never abandonment; §7.4).
_DEAD_LETTER_AFTER_ATTEMPTS = 10
# Reclaim expired leases at most this often (cheap relative to the claim
# loop, so it just runs every cycle rather than on a separate timer).


def _backoff_seconds(attempt: int) -> float:
    """Exponential-with-full-jitter delay for the given 1-based attempt count."""
    capped = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2**attempt))
    return capped * random.uniform(0.5, 1.0)


def _redact_for_log(value: str) -> str:
    """Best-effort scrub of a log-bound string using the shared key-substring denylist.

    Only used for values that are already short, sanitized classification
    tokens (error codes) — never the payload body, signature, secret, or
    full URL, which this module's log lines never include in the first
    place (see :func:`_log_attempt`).
    """
    lowered = value.lower()
    if any(s in lowered for s in _REDACT_KEY_SUBSTRINGS):
        return "[redacted]"
    return value


def _endpoint_host(endpoint: str) -> str:
    """Host-only projection of the target URL, for logging (never the full URL/query)."""
    return httpx.URL(endpoint).host


def _log_attempt(
    event: LifecycleOutboxEvent,
    *,
    outcome: str,
    latency_ms: float,
    http_status: int | None,
    error_code: str | None,
    endpoint_host: str | None,
) -> None:
    """One structured log line per delivery attempt (§9) — never the payload
    body, the signature, the secret, or the full target URL (host only)."""
    _logger.info(
        "manager_webhook delivery attempt",
        extra={
            "event_id": event.id,
            "workspace_id": event.workspace_id,
            "session_id": event.session_id,
            "sequence": event.sequence,
            "attempt_count": event.attempt_count,
            "event_type": event.event_type,
            "outcome": outcome,
            "latency_ms": round(latency_ms, 1),
            "http_status": http_status,
            "error_code": _redact_for_log(error_code) if error_code else None,
            "endpoint_host": endpoint_host,
        },
    )


async def _deliver_one(
    client: httpx.AsyncClient,
    store: SessionLifecycleStore,
    event: LifecycleOutboxEvent,
    *,
    endpoint: str,
    key_id: str | None,
) -> None:
    """Attempt one HTTP delivery for a claimed row and record the outcome."""
    # Wire envelope per §3.1 of the design doc — event.payload (stored) is
    # only the event-type-specific `data`; the transmitted body wraps it with
    # the stable identity/ordering fields the manager's dedupe/ordering
    # contract needs (event_id, sequence, ...). Signed over the exact bytes
    # transmitted, not the stored payload alone.
    raw_json_body = json.dumps(
        {
            "event_id": event.id,
            "event_version": event.event_version,
            "event_type": event.event_type,
            "workspace_id": event.workspace_id,
            "session_id": event.session_id,
            "sequence": event.sequence,
            "occurred_at": event.created_at,
            "data": json.loads(event.payload),
        }
    )
    secret = signing.current_signing_key()
    started = time.monotonic()
    if secret is None:
        # Configured enabled but no signing key in the environment — fail
        # this attempt like any other delivery failure (retried with
        # backoff); never send an unsigned request.
        store.mark_delivery_failed(
            event.id,
            workspace_id=event.workspace_id,
            next_attempt_at=int(time.time()) + int(_backoff_seconds(event.attempt_count)),
            dead_letter_after_attempts=_DEAD_LETTER_AFTER_ATTEMPTS,
            error_code="missing_signing_key",
            error_message=f"{signing.SECRET_ENV_VAR} is not set",
        )
        _log_attempt(
            event,
            outcome="error",
            latency_ms=0.0,
            http_status=None,
            error_code="missing_signing_key",
            endpoint_host=_endpoint_host(endpoint),
        )
        return
    timestamp = int(time.time())
    signature = signing.sign(
        secret=secret, timestamp=timestamp, event_id=event.id, raw_json_body=raw_json_body
    )
    headers = signing.build_headers(
        event_id=event.id,
        event_type=event.event_type,
        attempt=event.attempt_count,
        timestamp=timestamp,
        key_id=key_id,
        signature=signature,
    )
    try:
        response = await client.post(
            endpoint,
            content=raw_json_body,
            headers=headers,
            timeout=_DELIVERY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        latency_ms = (time.monotonic() - started) * 1000
        store.mark_delivery_failed(
            event.id,
            workspace_id=event.workspace_id,
            next_attempt_at=int(time.time()) + int(_backoff_seconds(event.attempt_count)),
            dead_letter_after_attempts=_DEAD_LETTER_AFTER_ATTEMPTS,
            error_code=type(exc).__name__,
            error_message=str(exc)[:256],
        )
        _log_attempt(
            event,
            outcome="transport_error",
            latency_ms=latency_ms,
            http_status=None,
            error_code=type(exc).__name__,
            endpoint_host=_endpoint_host(endpoint),
        )
        return
    latency_ms = (time.monotonic() - started) * 1000
    if 200 <= response.status_code < 300:
        store.mark_delivered(
            event.id,
            workspace_id=event.workspace_id,
            delivered_at=int(time.time()),
            http_status=response.status_code,
        )
        _log_attempt(
            event,
            outcome="delivered",
            latency_ms=latency_ms,
            http_status=response.status_code,
            error_code=None,
            endpoint_host=_endpoint_host(endpoint),
        )
        return
    store.mark_delivery_failed(
        event.id,
        workspace_id=event.workspace_id,
        next_attempt_at=int(time.time()) + int(_backoff_seconds(event.attempt_count)),
        dead_letter_after_attempts=_DEAD_LETTER_AFTER_ATTEMPTS,
        http_status=response.status_code,
        error_code="http_error",
        error_message=f"manager returned {response.status_code}",
    )
    _log_attempt(
        event,
        outcome="rejected",
        latency_ms=latency_ms,
        http_status=response.status_code,
        error_code="http_error",
        endpoint_host=_endpoint_host(endpoint),
    )


async def _run_once(
    client: httpx.AsyncClient, store: SessionLifecycleStore, *, lease_owner: str
) -> int:
    """One dispatcher cycle: reclaim expired leases, then claim and deliver.

    :returns: Number of rows claimed this cycle (0 means the caller should
        idle-sleep before the next cycle).
    """
    now = int(time.time())
    reclaimed = store.reclaim_expired_leases(now=now)
    if reclaimed:
        _logger.info("manager_webhook reclaimed %d expired lease(s)", reclaimed)
    try:
        config = manager_webhook_config()
    except ManagerWebhookConfigError:
        # create_app() already calls manager_webhook_config() once at boot
        # and lets this same exception type propagate (fail closed before
        # serving traffic) — so reaching this branch means the config file
        # was edited into a bad state on disk *after* a successful boot.
        # A running server must not crash on that; log once per cycle and
        # treat as disabled until the file is fixed.
        _logger.warning("manager_webhook config invalid; dispatcher idling", exc_info=True)
        return 0
    if not config.enabled or config.endpoint is None:
        return 0
    claimed = store.claim_batch(
        limit=_CLAIM_BATCH_SIZE, now=now, lease_owner=lease_owner, lease_seconds=_LEASE_SECONDS
    )
    for event in claimed:
        await _deliver_one(client, store, event, endpoint=config.endpoint, key_id=config.key_id)
    return len(claimed)


async def run_dispatcher(store: SessionLifecycleStore, *, lease_owner: str) -> None:
    """
    Run the dispatcher loop until cancelled.

    :param store: The session-lifecycle store to claim/deliver from.
    :param lease_owner: This replica's identity (e.g. a hostname+pid or
        random token), stamped on claimed rows' ``lease_owner``.
    """
    async with httpx.AsyncClient() as client:
        while True:
            try:
                claimed_count = await _run_once(client, store, lease_owner=lease_owner)
            except Exception:
                _logger.exception("manager_webhook dispatcher cycle failed")
                claimed_count = 0
            if claimed_count == 0:
                await asyncio.sleep(_IDLE_POLL_SECONDS)


async def cancel_dispatcher(task: asyncio.Task[None]) -> None:
    """Cancel and await the dispatcher task, swallowing the resulting CancelledError."""
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
