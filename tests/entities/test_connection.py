"""Tests for the ProviderConnection entity."""

from __future__ import annotations

from omnigent.entities import ProviderConnection


def test_repr_redacts_secret() -> None:
    # A frozen entity holding a decrypted token must never emit it via repr
    # (log lines, exception frames, APM captures).
    conn = ProviderConnection(
        user_id="alice",
        provider="github",
        account_id="",
        secret={"access_token": "ghu_supersecret", "refresh_token": "ghr_supersecret"},
        metadata={"github_login": "alice"},
        created_at=1,
        updated_at=2,
    )
    text = repr(conn)
    assert "ghu_supersecret" not in text
    assert "ghr_supersecret" not in text
    assert "<redacted>" in text
    assert "alice" in text  # non-secret fields still shown


def test_repr_distinguishes_empty_secret_from_metadata_only() -> None:
    # secret={} is a loaded (if empty) secret, not the metadata-only None view;
    # the repr must not conflate the two.
    loaded_empty = ProviderConnection(
        user_id="a",
        provider="github",
        account_id="",
        secret={},
        metadata={},
        created_at=1,
        updated_at=1,
    )
    metadata_only = ProviderConnection(
        user_id="a",
        provider="github",
        account_id="",
        secret=None,
        metadata={},
        created_at=1,
        updated_at=1,
    )
    assert "secret=<redacted>" in repr(loaded_empty)
    assert "secret=None" in repr(metadata_only)
