"""HMAC request signing for the manager-webhook delivery (OMN-104).

Stripe-style scheme: sign the raw transmitted body plus a timestamp,
verify before JSON parsing. No outbound-signing precedent existed in this
codebase to extend (the existing ``hmac_digest`` in
:mod:`omnigent.server.oidc` keys an in-process cache, not a wire
signature), so this is a fresh, narrow module — the single source of truth
for both signing (the dispatcher) and verification (documented for the
manager to implement; also used by this feature's own reference tests).

The secret is env-var only, never the config file — the two-secret
(current + previous) shape supports rotation: sign only with the current
secret, but verify against both while a rotation is in flight.
"""

from __future__ import annotations

import hashlib
import hmac
import os

_SECRET_ENV = "OMNIGENT_MANAGER_WEBHOOK_SECRET"
_SECRET_PREVIOUS_ENV = "OMNIGENT_MANAGER_WEBHOOK_SECRET_PREVIOUS"

# Recommended replay-window tolerance (seconds) — see module docstring on
# omnigent/server/manager_webhook_dispatcher.py for the delivery side and
# §8.2 of docs/architecture/2026-08-10-durable-session-lifecycle-push.md.
DEFAULT_TOLERANCE_SECONDS = 300


def current_secret() -> str | None:
    """The active signing secret, or ``None`` if unset (signing disabled)."""
    value = os.environ.get(_SECRET_ENV)
    return value if value else None


def previous_secret() -> str | None:
    """The previous secret during a rotation window, or ``None``."""
    value = os.environ.get(_SECRET_PREVIOUS_ENV)
    return value if value else None


def canonicalize(*, timestamp: int, event_id: str, raw_json_body: str) -> str:
    """Build the signed-content string: ``{timestamp}.{event_id}.{raw_json_body}``."""
    return f"{timestamp}.{event_id}.{raw_json_body}"


def sign(*, secret: str, timestamp: int, event_id: str, raw_json_body: str) -> str:
    """
    Compute the ``X-Omnigent-Signature`` header value for one delivery attempt.

    :param secret: The signing secret (:func:`current_secret` — never
        *previous*; only the current secret ever signs).
    :param timestamp: Unix epoch seconds at signing time.
    :param event_id: The event's stable ``event_id``.
    :param raw_json_body: The exact bytes (as a str) transmitted as the
        request body — signing must happen over what's actually sent, not
        a re-serialization of it.
    :returns: ``"v1=<hex hmac-sha256>"``.
    """
    signed_content = canonicalize(
        timestamp=timestamp, event_id=event_id, raw_json_body=raw_json_body
    )
    digest = hmac.new(
        secret.encode("utf-8"), signed_content.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"v1={digest}"


def verify(
    *,
    signature_header: str,
    timestamp: int,
    event_id: str,
    raw_json_body: str,
    now: int,
    secrets: list[str],
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """
    Verify a received delivery's signature and timestamp freshness.

    Reference implementation for the manager side of the contract (also
    exercised directly by this feature's signature/redaction tests). Checks
    timestamp tolerance *and* signature — a stale timestamp is rejected even
    with an otherwise-valid signature, and vice versa.

    :param signature_header: The received ``X-Omnigent-Signature`` value,
        e.g. ``"v1=abcdef..."``.
    :param timestamp: The received ``X-Omnigent-Timestamp`` value.
    :param event_id: The received ``X-Omnigent-Event-Id`` value.
    :param raw_json_body: The exact received request body bytes (as a str),
        read BEFORE JSON parsing.
    :param now: The verifier's own current Unix epoch seconds.
    :param secrets: Candidate secrets to verify against (current + previous,
        for a rotation window). Empty/falsy entries are ignored.
    :param tolerance_seconds: Maximum allowed clock skew between *timestamp*
        and *now*.
    :returns: ``True`` only if the timestamp is within tolerance AND the
        signature matches one of *secrets*.
    """
    if abs(now - timestamp) > tolerance_seconds:
        return False
    for secret in secrets:
        if not secret:
            continue
        expected = sign(
            secret=secret, timestamp=timestamp, event_id=event_id, raw_json_body=raw_json_body
        )
        if hmac.compare_digest(signature_header, expected):
            return True
    return False


def build_headers(
    *,
    event_id: str,
    event_type: str,
    attempt: int,
    timestamp: int,
    key_id: str | None,
    signature: str,
) -> dict[str, str]:
    """
    Build the full header set for one delivery attempt.

    :param event_id: The event's stable ``event_id``.
    :param event_type: e.g. ``"session.completed"``.
    :param attempt: 1-based delivery attempt count.
    :param timestamp: Unix epoch seconds at signing time (same value used in
        :func:`sign`).
    :param key_id: ``manager_webhook.key_id`` from config, or ``None``.
    :param signature: The value returned by :func:`sign`.
    :returns: Header dict ready to attach to the outbound HTTP request.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Omnigent-Event-Id": event_id,
        "X-Omnigent-Event-Type": event_type,
        "X-Omnigent-Delivery-Attempt": str(attempt),
        "X-Omnigent-Timestamp": str(timestamp),
        "X-Omnigent-Signature": signature,
    }
    if key_id:
        headers["X-Omnigent-Key-Id"] = key_id
    return headers
