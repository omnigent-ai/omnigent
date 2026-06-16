"""Tests for accounts auth route helper functions.

The full accounts auth flow requires an account store with passwords,
so we test the pure-function helpers directly.
"""

from __future__ import annotations

from omnigent.server.routes.accounts_auth import (
    _redact_for_log,
    _validate_username,
)


class TestValidateUsername:
    """Tests for username format and reserved-name checks."""

    def test_valid_lowercase(self) -> None:
        assert _validate_username("alice") is None

    def test_valid_with_dots(self) -> None:
        assert _validate_username("alice.bob") is None

    def test_valid_with_hyphens(self) -> None:
        assert _validate_username("alice-bob") is None

    def test_valid_email(self) -> None:
        assert _validate_username("alice@example.com") is None

    def test_reserved_local(self) -> None:
        result = _validate_username("local")
        assert result is not None
        assert "reserved" in result

    def test_reserved_public(self) -> None:
        result = _validate_username("__public__")
        assert result is not None
        assert "reserved" in result

    def test_uppercase_lowercased_then_valid(self) -> None:
        # _validate_username lowercases before checking, so ALICE -> alice is valid
        result = _validate_username("ALICE")
        assert result is None

    def test_empty_string(self) -> None:
        result = _validate_username("")
        assert result is not None

    def test_special_chars_rejected(self) -> None:
        result = _validate_username("alice<script>")
        assert result is not None


class TestRedactForLog:
    """Tests for log redaction of user IDs."""

    def test_short_id(self) -> None:
        assert _redact_for_log("ab") == "a***"

    def test_normal_id(self) -> None:
        result = _redact_for_log("alice@example.com")
        assert result.startswith("ali")
        assert "***" in result
        assert "len=" in result

    def test_min_length(self) -> None:
        assert _redact_for_log("a") == "a***"
