"""Tests for OIDC auth route helper functions.

The OIDC routes themselves require an external IdP, so we test the
pure-function helpers directly instead of via HTTP.
"""

from __future__ import annotations

import time

from omnigent.server.routes.auth import (
    _claim_is_verified_true,
    _CliTicket,
    _evict_expired_tickets,
    _sanitize_return_to,
)

# ── _sanitize_return_to ──────────────────────────────────────────────


class TestSanitizeReturnTo:
    """Tests for the open-redirect prevention helper."""

    def test_none_returns_root(self) -> None:
        assert _sanitize_return_to(None) == "/"

    def test_empty_returns_root(self) -> None:
        assert _sanitize_return_to("") == "/"

    def test_relative_path_preserved(self) -> None:
        assert _sanitize_return_to("/sessions/abc") == "/sessions/abc"

    def test_relative_path_with_query_preserved(self) -> None:
        assert _sanitize_return_to("/sessions/abc?tab=files") == "/sessions/abc?tab=files"

    def test_absolute_url_rejected(self) -> None:
        assert _sanitize_return_to("https://evil.example") == "/"

    def test_protocol_relative_rejected(self) -> None:
        assert _sanitize_return_to("//evil.example") == "/"

    def test_backslash_protocol_relative_rejected(self) -> None:
        assert _sanitize_return_to("/\\evil.example") == "/"

    def test_no_leading_slash_rejected(self) -> None:
        assert _sanitize_return_to("sessions/abc") == "/"


# ── _evict_expired_tickets ───────────────────────────────────────────


class TestEvictExpiredTickets:
    """Tests for CLI ticket expiry."""

    def test_evicts_old_tickets(self) -> None:
        tickets = {
            "old": _CliTicket(created_at=time.time() - 600),
            "fresh": _CliTicket(created_at=time.time()),
        }
        _evict_expired_tickets(tickets)
        assert "old" not in tickets
        assert "fresh" in tickets

    def test_empty_dict_no_crash(self) -> None:
        tickets: dict[str, _CliTicket] = {}
        _evict_expired_tickets(tickets)
        assert len(tickets) == 0


# ── _claim_is_verified_true ──────────────────────────────────────────


class TestClaimIsVerifiedTrue:
    """Tests for the email_verified claim check."""

    def test_true_bool(self) -> None:
        assert _claim_is_verified_true(True) is True

    def test_true_string(self) -> None:
        assert _claim_is_verified_true("true") is True

    def test_true_string_mixed_case(self) -> None:
        assert _claim_is_verified_true("True") is True
        assert _claim_is_verified_true(" TRUE ") is True

    def test_false_bool(self) -> None:
        assert _claim_is_verified_true(False) is False

    def test_false_string(self) -> None:
        assert _claim_is_verified_true("false") is False

    def test_none(self) -> None:
        assert _claim_is_verified_true(None) is False

    def test_random_string(self) -> None:
        assert _claim_is_verified_true("yes") is False
