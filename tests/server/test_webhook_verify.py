"""Unit tests for inbound-webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac

from omnigent.server.webhook_verify import (
    verify_bearer,
    verify_github_signature,
    verify_slack_signature,
)


def _gh_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _slack_sig(secret: str, ts: str, body: bytes) -> str:
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_github_signature() -> None:
    body = b'{"x":1}'
    assert verify_github_signature("s3cret", body, _gh_sig("s3cret", body)) is True
    assert verify_github_signature("s3cret", body, _gh_sig("wrong", body)) is False
    assert verify_github_signature("s3cret", body, None) is False
    assert verify_github_signature("", body, _gh_sig("s3cret", body)) is False
    # Signature must cover the exact bytes.
    assert verify_github_signature("s3cret", b'{"x":2}', _gh_sig("s3cret", body)) is False


def test_slack_signature_valid_and_fresh() -> None:
    body = b"payload"
    ts = "1000"
    sig = _slack_sig("sign", ts, body)
    assert verify_slack_signature("sign", ts, body, sig, now=1000) is True
    # Within the 5-minute window.
    assert verify_slack_signature("sign", ts, body, sig, now=1000 + 120) is True


def test_slack_signature_rejects_stale_and_bad() -> None:
    body = b"payload"
    ts = "1000"
    sig = _slack_sig("sign", ts, body)
    assert verify_slack_signature("sign", ts, body, sig, now=1000 + 9999) is False  # stale
    assert (
        verify_slack_signature("sign", ts, body, _slack_sig("nope", ts, body), now=1000) is False
    )
    assert verify_slack_signature("sign", None, body, sig, now=1000) is False
    assert verify_slack_signature("sign", "notanint", body, sig, now=1000) is False


def test_bearer() -> None:
    assert verify_bearer("tok", "Bearer tok") is True
    assert verify_bearer("tok", "Bearer nope") is False
    assert verify_bearer("tok", "tok") is False  # missing prefix
    assert verify_bearer("tok", None) is False
    assert verify_bearer("", "Bearer ") is False
