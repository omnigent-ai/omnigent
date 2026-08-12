"""Tests for the manager-webhook HMAC signing scheme and its config loading.

Covers ``omnigent.server.manager_webhook_signing`` (sign/verify/build_headers,
the Stripe-style canonicalization) and the ``manager_webhook`` block of
``omnigent.server.server_config`` (fail-closed HTTPS enforcement). See
``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §8.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.server import manager_webhook_signing as signing
from omnigent.server import server_config

# ── sign() ──────────────────────────────────────────────────────


def test_sign_is_deterministic() -> None:
    a = signing.sign(secret="s3cr3t", timestamp=1000, event_id="evt1", raw_json_body='{"a":1}')
    b = signing.sign(secret="s3cr3t", timestamp=1000, event_id="evt1", raw_json_body='{"a":1}')
    assert a == b


@pytest.mark.parametrize(
    "kwargs",
    [
        {"secret": "different"},
        {"timestamp": 1001},
        {"event_id": "evt2"},
        {"raw_json_body": '{"a":2}'},
    ],
)
def test_sign_changes_with_any_input(kwargs: dict[str, object]) -> None:
    base = {
        "secret": "s3cr3t",
        "timestamp": 1000,
        "event_id": "evt1",
        "raw_json_body": '{"a":1}',
    }
    baseline = signing.sign(**base)
    varied = {**base, **kwargs}
    assert signing.sign(**varied) != baseline


def test_sign_output_format() -> None:
    value = signing.sign(secret="s3cr3t", timestamp=1000, event_id="evt1", raw_json_body="{}")
    assert value.startswith("v1=")
    digest = value.removeprefix("v1=")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# ── verify() ────────────────────────────────────────────────────


def test_verify_accepts_correctly_signed_fresh_request() -> None:
    now = 1_800_000_000
    signature = signing.sign(secret="s3cr3t", timestamp=now, event_id="evt1", raw_json_body="{}")
    assert signing.verify(
        signature_header=signature,
        timestamp=now,
        event_id="evt1",
        raw_json_body="{}",
        now=now,
        secrets=["s3cr3t"],
    )


def test_verify_rejects_tampered_body() -> None:
    now = 1_800_000_000
    signature = signing.sign(
        secret="s3cr3t", timestamp=now, event_id="evt1", raw_json_body='{"a":1}'
    )
    assert not signing.verify(
        signature_header=signature,
        timestamp=now,
        event_id="evt1",
        raw_json_body='{"a":2}',
        now=now,
        secrets=["s3cr3t"],
    )


def test_verify_rejects_missing_signature() -> None:
    """An empty/absent ``X-Omnigent-Signature`` header must not verify.

    Distinct from "tampered" (a syntactically-shaped but wrong signature) —
    this is what a receiver sees when the header is missing entirely, e.g.
    ``request.headers.get("X-Omnigent-Signature", "")``. The acceptance
    contract requires both missing AND invalid HMAC rejection.
    """
    now = 1_800_000_000
    assert not signing.verify(
        signature_header="",
        timestamp=now,
        event_id="evt1",
        raw_json_body="{}",
        now=now,
        secrets=["s3cr3t"],
    )


def test_verify_rejects_malformed_signature_header() -> None:
    """A present but malformed header (wrong prefix/shape) must not verify."""
    now = 1_800_000_000
    assert not signing.verify(
        signature_header="not-a-real-signature",
        timestamp=now,
        event_id="evt1",
        raw_json_body="{}",
        now=now,
        secrets=["s3cr3t"],
    )


def test_verify_rejects_tampered_event_id() -> None:
    now = 1_800_000_000
    signature = signing.sign(secret="s3cr3t", timestamp=now, event_id="evt1", raw_json_body="{}")
    assert not signing.verify(
        signature_header=signature,
        timestamp=now,
        event_id="evt2",
        raw_json_body="{}",
        now=now,
        secrets=["s3cr3t"],
    )


def test_verify_rejects_timestamp_substitution() -> None:
    """A signature computed for one timestamp must not verify against another.

    Even though nothing about the header's *format* is wrong, the
    canonicalized content differs, so the digest cannot match.
    """
    signed_at = 1_800_000_000
    signature = signing.sign(
        secret="s3cr3t", timestamp=signed_at, event_id="evt1", raw_json_body="{}"
    )
    assert not signing.verify(
        signature_header=signature,
        timestamp=signed_at + 1,
        event_id="evt1",
        raw_json_body="{}",
        now=signed_at + 1,
        secrets=["s3cr3t"],
    )


def test_verify_rejects_stale_timestamp_even_with_valid_signature() -> None:
    """The specific acceptance-test requirement: a stale timestamp is rejected
    even when the signature is otherwise mathematically correct for it."""
    signed_at = 1_800_000_000
    signature = signing.sign(
        secret="s3cr3t", timestamp=signed_at, event_id="evt1", raw_json_body="{}"
    )
    far_future = signed_at + signing.DEFAULT_TOLERANCE_SECONDS + 1
    assert not signing.verify(
        signature_header=signature,
        timestamp=signed_at,
        event_id="evt1",
        raw_json_body="{}",
        now=far_future,
        secrets=["s3cr3t"],
    )


def test_verify_tolerance_boundary() -> None:
    signed_at = 1_800_000_000
    signature = signing.sign(
        secret="s3cr3t", timestamp=signed_at, event_id="evt1", raw_json_body="{}"
    )
    tolerance = 300
    # Exactly at the boundary: abs(now - timestamp) == tolerance -> accepted.
    at_boundary = signing.verify(
        signature_header=signature,
        timestamp=signed_at,
        event_id="evt1",
        raw_json_body="{}",
        now=signed_at + tolerance,
        secrets=["s3cr3t"],
        tolerance_seconds=tolerance,
    )
    assert at_boundary
    # One second past -> rejected.
    past_boundary = signing.verify(
        signature_header=signature,
        timestamp=signed_at,
        event_id="evt1",
        raw_json_body="{}",
        now=signed_at + tolerance + 1,
        secrets=["s3cr3t"],
        tolerance_seconds=tolerance,
    )
    assert not past_boundary


def test_verify_supports_secret_rotation() -> None:
    now = 1_800_000_000
    signature = signing.sign(secret="new", timestamp=now, event_id="evt1", raw_json_body="{}")
    # Verifying against both old+new (rotation window) succeeds.
    assert signing.verify(
        signature_header=signature,
        timestamp=now,
        event_id="evt1",
        raw_json_body="{}",
        now=now,
        secrets=["old", "new"],
    )
    # Verifying against only the old secret fails.
    assert not signing.verify(
        signature_header=signature,
        timestamp=now,
        event_id="evt1",
        raw_json_body="{}",
        now=now,
        secrets=["old"],
    )


def test_verify_ignores_empty_secret_candidates() -> None:
    now = 1_800_000_000
    signature = signing.sign(secret="s3cr3t", timestamp=now, event_id="evt1", raw_json_body="{}")
    assert signing.verify(
        signature_header=signature,
        timestamp=now,
        event_id="evt1",
        raw_json_body="{}",
        now=now,
        secrets=["", "s3cr3t"],
    )


# ── current_signing_key() / previous_secret() ───────────────────


def test_current_signing_key_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(signing.SECRET_ENV_VAR, "abc123")
    assert signing.current_signing_key() == "abc123"


def test_current_signing_key_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(signing.SECRET_ENV_VAR, raising=False)
    assert signing.current_signing_key() is None


def test_previous_secret_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(signing.PREVIOUS_SECRET_ENV_VAR, "old123")
    assert signing.previous_secret() == "old123"


def test_previous_secret_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(signing.PREVIOUS_SECRET_ENV_VAR, raising=False)
    assert signing.previous_secret() is None


# ── build_headers() ─────────────────────────────────────────────


def test_build_headers_includes_all_required_headers() -> None:
    headers = signing.build_headers(
        event_id="evt1",
        event_type="session.completed",
        attempt=2,
        timestamp=1000,
        key_id="key-a",
        signature="v1=deadbeef",
    )
    assert headers == {
        "Content-Type": "application/json",
        "X-Omnigent-Event-Id": "evt1",
        "X-Omnigent-Event-Type": "session.completed",
        "X-Omnigent-Delivery-Attempt": "2",
        "X-Omnigent-Timestamp": "1000",
        "X-Omnigent-Signature": "v1=deadbeef",
        "X-Omnigent-Key-Id": "key-a",
    }


def test_build_headers_omits_key_id_when_none() -> None:
    headers = signing.build_headers(
        event_id="evt1",
        event_type="session.completed",
        attempt=1,
        timestamp=1000,
        key_id=None,
        signature="v1=deadbeef",
    )
    assert "X-Omnigent-Key-Id" not in headers


# ── manager_webhook_config() ────────────────────────────────────


def _write_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict[str, object]
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config_path))


def test_manager_webhook_config_default_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = server_config.manager_webhook_config()
    assert cfg.enabled is False
    assert cfg.endpoint is None


def test_manager_webhook_config_https_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        {"manager_webhook": {"enabled": True, "endpoint": "https://manager.example.com/hook"}},
    )
    cfg = server_config.manager_webhook_config()
    assert cfg.enabled is True
    assert cfg.endpoint == "https://manager.example.com/hook"


def test_manager_webhook_config_http_without_override_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        {"manager_webhook": {"enabled": True, "endpoint": "http://manager.example.com/hook"}},
    )
    with pytest.raises(server_config.ManagerWebhookConfigError):
        server_config.manager_webhook_config()


def test_manager_webhook_config_http_with_dev_override_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "manager_webhook": {
                "enabled": True,
                "endpoint": "http://localhost:9999/hook",
                "allow_insecure_dev_endpoint": True,
            }
        },
    )
    cfg = server_config.manager_webhook_config()
    assert cfg.enabled is True
    assert cfg.endpoint == "http://localhost:9999/hook"


def test_manager_webhook_config_enabled_without_endpoint_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch, {"manager_webhook": {"enabled": True}})
    with pytest.raises(server_config.ManagerWebhookConfigError):
        server_config.manager_webhook_config()


def test_manager_webhook_config_key_id_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "manager_webhook": {
                "enabled": True,
                "endpoint": "https://manager.example.com/hook",
                "key_id": "key-2026-08",
            }
        },
    )
    cfg = server_config.manager_webhook_config()
    assert cfg.key_id == "key-2026-08"
