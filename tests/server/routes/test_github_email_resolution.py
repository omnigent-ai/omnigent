"""Tests for ``_resolve_github_email`` — the GitHub OIDC email resolver.

The resolved email becomes the sign-in identity (cookie ``sub``, admission
allowlist key, admin-list key), so it must be a *verified* address the
caller actually owns. GitHub's ``/user.email`` profile field is unverified
and attacker-settable, so it must never be used as a fallback identity —
this mirrors the ``email_verified`` gate the OIDC ``id_token`` path already
enforces (see ``test_oidc_callback.py``).
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.routes.auth import _GITHUB_EMAILS_ENDPOINT, _resolve_github_email


def _client(
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


@pytest.mark.asyncio
async def test_returns_primary_verified_email() -> None:
    """The primary, verified address from ``/user/emails`` is the identity."""
    async with _client(
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
async def test_unverified_profile_email_is_not_trusted() -> None:
    """No verified primary → return None, never the unverified profile email.

    Regression for an identity-spoofing / admin-takeover gap: when
    ``/user/emails`` yields no primary+verified entry, the resolver used to
    fall back to ``GET /user`` and return its (unverified, attacker-set)
    ``email``. That value is the sign-in identity, so it must not be trusted.
    """
    async with _client(
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
async def test_emails_endpoint_unavailable_returns_none() -> None:
    """If ``/user/emails`` is unavailable, fail closed (no profile fallback).

    Missing ``user:email`` scope makes ``/user/emails`` 403/404. The old
    fallback then returned the unverifiable profile email; the resolver must
    instead return None so the caller rejects the login.
    """
    async with _client(
        emails=httpx.Response(403, json={"message": "scope missing"}),
        profile=httpx.Response(200, json={"email": "victim@allowed-corp.com"}),
    ) as client:
        email = await _resolve_github_email(client, "tok")

    assert email is None


def test_emails_endpoint_constant_is_user_emails() -> None:
    """Guard: the resolver queries the verified-email list endpoint."""
    assert _GITHUB_EMAILS_ENDPOINT.endswith("/user/emails")
