"""Tests for OIDC auth route helper functions.

The OIDC routes themselves require an external IdP, so we test the
pure-function helpers directly instead of via HTTP.
"""

from __future__ import annotations

import time

import httpx
import pytest

from omnigent.server.routes.auth import (
    _claim_is_verified_true,
    _CliTicket,
    _evict_expired_tickets,
    _GITHUB_EMAILS_ENDPOINT,
    _resolve_github_email,
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


# ── _resolve_github_email ────────────────────────────────────────────


def _github_client(
    *,
    emails: httpx.Response,
    profile: httpx.Response,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose ``/user/emails`` and ``/user`` are mocked.

    :param emails: Response for ``GET /user/emails``.
    :param profile: Response for ``GET /user`` (the profile fallback that
        the resolver must NOT trust).
    :returns: A client backed by a :class:`httpx.MockTransport`.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/emails"):
            return emails
        if request.url.path.endswith("/user"):
            return profile
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


class TestResolveGithubEmail:
    """Tests for ``_resolve_github_email`` — the GitHub OIDC email resolver.

    The resolved email becomes the sign-in identity (cookie ``sub``,
    admission allowlist key, admin-list key), so it must be a *verified*
    address the caller actually owns. GitHub's ``/user`` profile field is
    unverified and attacker-settable, so it must never be used as a
    fallback identity — this mirrors the ``email_verified`` gate the OIDC
    ``id_token`` path already enforces (see :class:`TestClaimIsVerifiedTrue`).
    """

    @pytest.mark.asyncio
    async def test_returns_primary_verified_email(self) -> None:
        """The primary, verified address from ``/user/emails`` is the identity."""
        async with _github_client(
            emails=httpx.Response(
                200,
                json=[
                    {"email": "secondary@example.com", "primary": False, "verified": True},
                    {"email": "real@example.com", "primary": True, "verified": True},
                ],
            ),
            profile=httpx.Response(200, json={"email": "profile@example.com"}),
        ) as client:
            email = await _resolve_github_email(client, "tok")

        assert email == "real@example.com"

    @pytest.mark.asyncio
    async def test_unverified_profile_email_is_not_trusted(self) -> None:
        """No verified primary → return None, never the unverified profile email.

        Regression for an identity-spoofing / admin-takeover gap: when
        ``/user/emails`` yields no primary+verified entry, the resolver used
        to fall back to ``GET /user`` and return its (unverified,
        attacker-set) ``email``. That value is the sign-in identity, so it
        must not be trusted.
        """
        async with _github_client(
            emails=httpx.Response(
                200,
                json=[
                    # Primary but NOT verified, plus a verified-but-not-primary.
                    {"email": "attacker@example.com", "primary": True, "verified": False},
                    {"email": "other@example.com", "primary": False, "verified": True},
                ],
            ),
            profile=httpx.Response(200, json={"email": "victim@allowed-corp.com"}),
        ) as client:
            email = await _resolve_github_email(client, "tok")

        assert email is None, "an unverified profile email must never be the identity"

    @pytest.mark.asyncio
    async def test_emails_endpoint_unavailable_returns_none(self) -> None:
        """If ``/user/emails`` is unavailable, fail closed (no profile fallback).

        Missing ``user:email`` scope makes ``/user/emails`` 403/404. The old
        fallback then returned the unverifiable profile email; the resolver
        must instead return None so the caller rejects the login.
        """
        async with _github_client(
            emails=httpx.Response(403, json={"message": "scope missing"}),
            profile=httpx.Response(200, json={"email": "victim@allowed-corp.com"}),
        ) as client:
            email = await _resolve_github_email(client, "tok")

        assert email is None

    def test_emails_endpoint_constant_is_user_emails(self) -> None:
        """Guard: the resolver queries the verified-email list endpoint."""
        assert _GITHUB_EMAILS_ENDPOINT.endswith("/user/emails")
