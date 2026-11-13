"""Resolve a replacement bearer before retiring a working tunnel."""

from __future__ import annotations

import pytest

from omnigent.runner import _entry


@pytest.mark.parametrize("failure", ["missing", "error"])
def test_proactive_refresh_preserves_bootstrap_until_replacement_is_available(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    available = False

    def provider() -> str | None:
        if available:
            return "renewed-token"
        if failure == "error":
            raise OSError("credential service unavailable")
        return None

    # Credential discovery represents external SDK/OIDC storage, not the factory.
    monkeypatch.setattr(_entry, "_make_auth_token_factory", lambda *args, **kwargs: provider)
    factory = _entry._InitialAuthTokenFactory("bootstrap", "https://example.invalid")
    assert factory() == "bootstrap"
    if failure == "error":
        with pytest.raises(OSError, match="credential service unavailable"):
            factory.refresh()
    else:
        assert factory.refresh() is None
    assert factory() == "bootstrap"

    available = True
    assert factory.refresh() == "renewed-token"
    assert factory() == "renewed-token"


def test_failed_proactive_refresh_does_not_restore_a_rejected_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_entry, "_make_auth_token_factory", lambda *args, **kwargs: lambda: None)
    factory = _entry._InitialAuthTokenFactory("rejected", "https://example.invalid")
    factory.invalidate()
    assert factory.refresh() is None
    assert factory() is None


def test_deferred_renewal_leaves_the_relogin_error_for_a_real_rejection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bootstrap still works, so a failed renewal must not ask for a re-login."""
    monkeypatch.setattr(_entry, "_make_auth_token_factory", lambda *args, **kwargs: None)
    monkeypatch.setattr(_entry, "_AUTH_DISCOVERY_RETRY_INTERVAL_S", 0.0)
    factory = _entry._InitialAuthTokenFactory("bootstrap", "https://example.invalid")

    with caplog.at_level("ERROR", logger=_entry.__name__):
        assert factory.refresh() is None
        assert factory.refresh() is None
        assert factory() == "bootstrap"
        assert "databricks auth login" not in caplog.text

        factory.invalidate()
        assert factory() is None
    assert caplog.text.count("databricks auth login") == 1
