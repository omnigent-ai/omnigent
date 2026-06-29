"""Inbound-webhook signature verification (pure, dependency-free).

Used by the work-item intake endpoint to authenticate external senders:

- **GitHub** — ``X-Hub-Signature-256: sha256=<hmac>`` over the raw body.
- **Slack** — ``X-Slack-Signature: v0=<hmac>`` over ``v0:{ts}:{body}`` plus a
  freshness window on ``X-Slack-Request-Timestamp``.
- **Bearer** — a shared secret in ``Authorization: Bearer <secret>`` (the
  fallback for sources without their own signing scheme, e.g. Jira/email/generic).

All comparisons are constant-time. These functions take the secret explicitly
so they're trivially unit-testable; the route resolves secrets from env.
"""

from __future__ import annotations

import hashlib
import hmac
import time

# Reject Slack requests whose timestamp is older/newer than this (replay guard).
_SLACK_MAX_SKEW_SECONDS = 60 * 5


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verify a GitHub ``X-Hub-Signature-256`` header against the raw body.

    :param secret: The configured webhook secret.
    :param body: The exact raw request body bytes.
    :param signature_header: The ``sha256=<hex>`` header value, or ``None``.
    :returns: ``True`` iff the signature is present and valid.
    """
    if not secret or not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def verify_slack_signature(
    secret: str,
    timestamp: str | None,
    body: bytes,
    signature_header: str | None,
    *,
    now: float | None = None,
) -> bool:
    """Verify a Slack ``X-Slack-Signature`` (v0 scheme) with a freshness check.

    :param secret: The Slack signing secret.
    :param timestamp: The ``X-Slack-Request-Timestamp`` header (epoch seconds).
    :param body: The exact raw request body bytes.
    :param signature_header: The ``v0=<hex>`` header value, or ``None``.
    :param now: Current epoch seconds (injectable for tests).
    :returns: ``True`` iff fresh and validly signed.
    """
    if not secret or not timestamp or not signature_header:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > _SLACK_MAX_SKEW_SECONDS:
        return False
    basestring = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def verify_bearer(secret: str, authorization_header: str | None) -> bool:
    """Verify an ``Authorization: Bearer <secret>`` header (constant-time).

    :param secret: The configured shared intake secret.
    :param authorization_header: The ``Authorization`` header value, or ``None``.
    :returns: ``True`` iff the bearer token matches the secret.
    """
    if not secret or not authorization_header:
        return False
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        return False
    return hmac.compare_digest(authorization_header[len(prefix) :], secret)
