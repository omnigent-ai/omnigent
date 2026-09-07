from __future__ import annotations

import pytest
from omnigent_slack.enrollment_state import (
    StateError,
    emails_match,
    sign_state,
    verify_state,
)

_SECRET = "test-state-secret"
_EMAIL = "user@example.com"


def test_sign_verify_roundtrip() -> None:
    state = sign_state(
        "T123", "U456", _EMAIL, _SECRET, nonce="nonce-1", team_name="Acme", issued_at=1000
    )
    result = verify_state(state, _SECRET, now=1000)
    assert result.team_id == "T123"
    assert result.user_id == "U456"
    assert result.email == _EMAIL
    assert result.team_name == "Acme"
    assert result.nonce == "nonce-1"
    assert result.issued_at == 1000


def test_verify_rejects_wrong_secret() -> None:
    state = sign_state("T1", "U1", _EMAIL, _SECRET, nonce="n", issued_at=1000)
    with pytest.raises(StateError):
        verify_state(state, "different-secret", now=1000)


def test_verify_rejects_tampered_payload() -> None:
    state = sign_state("T1", "U1", _EMAIL, _SECRET, nonce="n", issued_at=1000)
    payload_b64, sig = state.split(".", 1)
    # Flip a character in the payload — signature no longer matches. In
    # particular the signed email can't be swapped without breaking the MAC.
    tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B") + "." + sig
    with pytest.raises(StateError):
        verify_state(tampered, _SECRET, now=1000)


def test_verify_rejects_expired() -> None:
    state = sign_state("T1", "U1", _EMAIL, _SECRET, nonce="n", issued_at=1000)
    with pytest.raises(StateError):
        verify_state(state, _SECRET, ttl_seconds=600, now=2000)


def test_verify_rejects_future_dated() -> None:
    state = sign_state("T1", "U1", _EMAIL, _SECRET, nonce="n", issued_at=5000)
    with pytest.raises(StateError):
        verify_state(state, _SECRET, ttl_seconds=600, now=1000)


def test_verify_rejects_malformed() -> None:
    with pytest.raises(StateError):
        verify_state("not-a-valid-token", _SECRET, now=1000)


def test_emails_match_case_and_whitespace_insensitive() -> None:
    assert emails_match("User@Example.com", " user@example.com ")
    assert not emails_match("a@example.com", "b@example.com")
    assert not emails_match("", "user@example.com")


def test_emails_match_handles_non_ascii() -> None:
    # Internationalized emails must not raise (hmac.compare_digest rejects
    # non-ASCII str) — they'd 500 the callback and block enrollment otherwise.
    assert emails_match("Björn@example.com", "björn@example.com")
    assert not emails_match("björn@example.com", "bjorn@example.com")
