"""Unit tests for OIDC authentication: OIDCConfig, UnifiedAuthProvider,
create_auth_provider factory, PKCE helpers, and session cookie utilities.

Tests mirror the source at ``omnigent/server/oidc.py`` and
``omnigent/server/auth.py``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import jwt
import pytest

from omnigent.server.auth import (
    RESERVED_USER_LOCAL,
    UnifiedAuthProvider,
    create_auth_provider,
)
from omnigent.server.oidc import (
    OIDCConfig,
    derive_code_challenge,
    generate_code_verifier,
    hmac_digest,
    mint_session_cookie,
)

# ── PKCE helpers ─────────────────────────────────────────────────


def test_generate_code_verifier_length() -> None:
    """Code verifier is within the RFC 7636 length bounds (43–128 chars).

    A verifier outside this range would be rejected by compliant
    IdPs during the token exchange.
    """
    verifier = generate_code_verifier()
    # RFC 7636 requires 43–128 characters.
    assert 43 <= len(verifier) <= 128, (
        f"Code verifier length {len(verifier)} is outside RFC 7636 bounds (43–128)."
    )


def test_derive_code_challenge_is_deterministic() -> None:
    """Same verifier always produces the same S256 challenge.

    Non-deterministic challenges would fail the PKCE verification
    at the IdP's token endpoint.
    """
    verifier = "test_verifier_12345678901234567890123456789012345"
    c1 = derive_code_challenge(verifier)
    c2 = derive_code_challenge(verifier)
    assert c1 == c2, "Code challenge must be deterministic."
    # Must be base64url without padding.
    assert "=" not in c1, "Code challenge must not have base64 padding."
    assert "+" not in c1, "Code challenge must use base64url, not base64."


def test_derive_code_challenge_differs_for_different_verifiers() -> None:
    """Different verifiers produce different challenges.

    If they didn't, PKCE would provide no security — any
    code_verifier would match any challenge.
    """
    v1 = generate_code_verifier()
    v2 = generate_code_verifier()
    assert derive_code_challenge(v1) != derive_code_challenge(v2)


# ── Session cookie minting / validation ──────────────────────────


_TEST_SECRET = b"a" * 32  # 32 bytes — minimum valid secret


def test_mint_session_cookie_produces_valid_jwt() -> None:
    """Minted cookie is a valid HS256 JWT with the expected claims.

    If the cookie fails to decode, the OIDCAuthProvider would
    reject every request and return None (→ 401 for all users).
    """
    token = mint_session_cookie(
        user_id="alice@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="google",
    )
    payload = jwt.decode(token, _TEST_SECRET, algorithms=["HS256"])
    assert payload["sub"] == "alice@example.com"
    assert payload["provider"] == "google"
    # exp should be ~8 hours from now.
    assert payload["exp"] > time.time()
    assert payload["exp"] <= time.time() + (8 * 3600) + 5


def test_mint_session_cookie_rejected_with_wrong_secret() -> None:
    """Cookie signed with one secret fails validation with another.

    This is the core security property: a stolen cookie from one
    deployment cannot be used on another deployment with a different
    secret.
    """
    token = mint_session_cookie(
        user_id="alice@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="google",
    )
    wrong_secret = b"b" * 32
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, wrong_secret, algorithms=["HS256"])


def test_hmac_digest_is_deterministic() -> None:
    """Same token + secret always produces the same digest.

    The credential cache in UnifiedAuthProvider uses this as a
    cache key — non-deterministic digests would cause 100% cache
    misses.
    """
    d1 = hmac_digest("my-token", _TEST_SECRET)
    d2 = hmac_digest("my-token", _TEST_SECRET)
    assert d1 == d2


def test_hmac_digest_differs_for_different_tokens() -> None:
    """Different tokens produce different digests.

    If they didn't, the cache would return the wrong user for
    a different cookie.
    """
    d1 = hmac_digest("token-a", _TEST_SECRET)
    d2 = hmac_digest("token-b", _TEST_SECRET)
    assert d1 != d2


# ── UnifiedAuthProvider (header source) ──────────────────────────


def _mock_request(
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> MagicMock:
    """Build a minimal mock HTTPConnection.

    MagicMock is acceptable here: we only need ``request.headers``
    (a dict-like) and ``request.cookies`` (a dict-like), and
    ``HTTPConnection`` is a complex ASGI object that cannot be
    trivially constructed without a real scope.

    :param headers: Request headers dict.
    :param cookies: Request cookies dict.
    :returns: A mock with ``.headers`` and ``.cookies`` set.
    """
    mock = MagicMock()
    mock.headers = headers or {}
    mock.cookies = cookies or {}
    return mock


def test_header_source_returns_cloudflare_access_email_header() -> None:
    """Header source also accepts Cloudflare Access user email headers.

    Cloudflare Access injects ``Cf-Access-Authenticated-User-Email`` rather
    than ``X-Forwarded-Email``. Header mode should trust that value directly
    without requiring an extra proxy rewrite.
    """
    provider = UnifiedAuthProvider(source="header")
    request = _mock_request(
        headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"}
    )
    assert provider.get_user_id(request) == "alice@example.com"


def test_header_source_returns_email_from_header() -> None:
    """Header source extracts user ID from X-Forwarded-Email.

    This is the primary code path for Databricks Apps deployments
    where the proxy injects the header.
    """
    provider = UnifiedAuthProvider(source="header")
    request = _mock_request(headers={"X-Forwarded-Email": "alice@example.com"})
    assert provider.get_user_id(request) == "alice@example.com"


def test_header_source_rejects_missing_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing header is rejected (``None`` → 401) by default.

    A missing or proxy-dropped ``X-Forwarded-Email``
    must fail closed. If this returned ``"local"`` instead, every
    unauthenticated request would share one identity with OWNER
    access to every other unauthenticated user's sessions.
    """
    # The provider resolves OMNIGENT_LOCAL_SINGLE_USER at
    # construction; clear it so an ambient value (e.g. a dev shell
    # that ran a local server) can't flip this test to the
    # single-user fallback path.
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    provider = UnifiedAuthProvider(source="header")
    request = _mock_request()
    assert provider.get_user_id(request) is None


def test_header_source_local_single_user_falls_back_to_local() -> None:
    """Explicit single-user runtime keeps the ``"local"`` fallback.

    ``local_single_user=True`` models a server spawned with
    ``OMNIGENT_LOCAL_SINGLE_USER=1`` (the managed local spawn
    paths) — its only user IS the local user, and no proxy exists
    to inject identity.
    """
    provider = UnifiedAuthProvider(source="header", local_single_user=True)
    request = _mock_request()
    assert provider.get_user_id(request) == RESERVED_USER_LOCAL


def test_header_source_resolves_single_user_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``local_single_user=None`` resolves from the env at construction.

    The managed local spawn paths configure single-user mode via the
    ``OMNIGENT_LOCAL_SINGLE_USER=1`` env var, not a constructor
    argument — this pins the env-resolution path they rely on.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    provider = UnifiedAuthProvider(source="header")
    request = _mock_request()
    assert provider.get_user_id(request) == RESERVED_USER_LOCAL


def test_header_source_single_user_still_honors_header() -> None:
    """A present header wins over the single-user fallback.

    Even on a single-user local runtime, a proxy-injected identity
    must be honored (and reserved names rejected) — the fallback
    only covers the header-absent case.
    """
    provider = UnifiedAuthProvider(source="header", local_single_user=True)
    request = _mock_request(headers={"X-Forwarded-Email": "alice@example.com"})
    assert provider.get_user_id(request) == "alice@example.com"


@pytest.mark.parametrize("reserved", ["local", "__public__"])
def test_header_source_rejects_reserved_names(
    reserved: str,
) -> None:
    """Reserved names are rejected in header mode.

    If accepted, a client could impersonate the admin or public
    sentinel by sending ``X-Forwarded-Email: local``.
    """
    provider = UnifiedAuthProvider(source="header")
    request = _mock_request(headers={"X-Forwarded-Email": reserved})
    assert provider.get_user_id(request) is None


def test_header_source_login_url_is_none() -> None:
    """Header-only provider has no login URL.

    The frontend uses this to know it should NOT redirect to a
    login page when /v1/me returns null.
    """
    provider = UnifiedAuthProvider(source="header")
    assert provider.login_url is None


# ── UnifiedAuthProvider (oidc source) ────────────────────────────


def _make_oidc_config() -> OIDCConfig:
    """Build a minimal OIDCConfig for testing cookie validation."""
    return OIDCConfig(
        issuer="https://accounts.google.com",
        client_id="test-client",
        client_secret="test-secret",
        redirect_uri="https://app.example.com/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        userinfo_endpoint=None,
        allow_invites=False,
    )


def test_oidc_source_reads_valid_cookie() -> None:
    """OIDC source extracts user ID from a valid session cookie.

    This is the primary code path for standalone OIDC deployments
    where the server minted the cookie after a login flow.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    token = mint_session_cookie(
        user_id="alice@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="google",
    )
    request = _mock_request(cookies={config.session_cookie_name: token})
    assert provider.get_user_id(request) == "alice@example.com"


def test_oidc_source_returns_none_for_missing_cookie() -> None:
    """OIDC source returns None when no session cookie is present.

    This triggers the 401 + login_url response from /v1/me,
    which the frontend uses to redirect to the login page.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    request = _mock_request()
    assert provider.get_user_id(request) is None


def test_oidc_source_returns_none_for_tampered_cookie() -> None:
    """OIDC source rejects a cookie signed with a different secret.

    If it accepted tampered cookies, an attacker could forge
    arbitrary session cookies.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    # Sign with a different secret.
    wrong_secret = b"b" * 32
    token = mint_session_cookie(
        user_id="alice@example.com",
        cookie_secret=wrong_secret,
        ttl_hours=8,
        provider="google",
    )
    request = _mock_request(cookies={config.session_cookie_name: token})
    assert provider.get_user_id(request) is None


def test_oidc_source_returns_none_for_expired_cookie() -> None:
    """OIDC source rejects an expired session cookie.

    Expired cookies must force re-authentication through the
    login flow.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    # Mint a cookie that expired 1 second ago.
    payload = {
        "sub": "alice@example.com",
        "iat": int(time.time()) - 3600,
        "exp": int(time.time()) - 1,
        "provider": "google",
    }
    token = jwt.encode(payload, _TEST_SECRET, algorithm="HS256")
    request = _mock_request(cookies={config.session_cookie_name: token})
    assert provider.get_user_id(request) is None


@pytest.mark.parametrize("reserved", ["local", "__public__"])
def test_oidc_source_rejects_reserved_sub_claims(reserved: str) -> None:
    """OIDC source rejects cookies with reserved user names in sub.

    If a cookie somehow contained "local" or "__public__" as the
    sub claim, it must not be accepted — these are internal
    sentinel values.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    token = mint_session_cookie(
        user_id=reserved,
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="google",
    )
    request = _mock_request(cookies={config.session_cookie_name: token})
    assert provider.get_user_id(request) is None


def test_oidc_source_caches_validated_cookie() -> None:
    """OIDC source caches a validated cookie to avoid repeated JWT decode.

    The TTL credential cache prevents decoding the same cookie on
    every request. If caching is broken, JWT decode runs on every
    request — a significant performance regression under load.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    token = mint_session_cookie(
        user_id="alice@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="google",
    )
    request = _mock_request(cookies={config.session_cookie_name: token})

    # First call populates the cache.
    assert provider.get_user_id(request) == "alice@example.com"
    # Cache should now have one entry.
    assert len(provider._cookie_cache) == 1

    # Second call should use the cache (same result, no new entry).
    assert provider.get_user_id(request) == "alice@example.com"
    assert len(provider._cookie_cache) == 1


def test_oidc_source_accepts_bearer_token() -> None:
    """OIDC source accepts a session JWT via Authorization: Bearer header.

    This is the code path used by ``omnigent run --server`` after
    ``omnigent login`` stores a token. The CLI sends the JWT as
    a Bearer token instead of a cookie.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    token = mint_session_cookie(
        user_id="alice@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="google",
    )
    # No cookie, but Bearer header present.
    request = _mock_request(
        headers={"Authorization": f"Bearer {token}"},
    )
    assert provider.get_user_id(request) == "alice@example.com"


def test_oidc_source_cookie_takes_precedence_over_bearer() -> None:
    """When both cookie and Bearer token are present, cookie wins.

    The cookie is set by the browser login flow and is more
    authoritative. The Bearer token is a fallback for CLI clients.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    cookie_token = mint_session_cookie(
        user_id="cookie-user@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="google",
    )
    bearer_token = mint_session_cookie(
        user_id="bearer-user@example.com",
        cookie_secret=_TEST_SECRET,
        ttl_hours=8,
        provider="google",
    )
    request = _mock_request(
        cookies={config.session_cookie_name: cookie_token},
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    # Cookie user wins.
    assert provider.get_user_id(request) == "cookie-user@example.com"


def test_oidc_source_rejects_invalid_bearer_token() -> None:
    """OIDC source rejects a Bearer token signed with a wrong secret.

    An attacker sending a forged Bearer token must not gain access.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    wrong_secret = b"b" * 32
    token = mint_session_cookie(
        user_id="alice@example.com",
        cookie_secret=wrong_secret,
        ttl_hours=8,
        provider="google",
    )
    request = _mock_request(
        headers={"Authorization": f"Bearer {token}"},
    )
    assert provider.get_user_id(request) is None


def test_oidc_source_login_url() -> None:
    """OIDC provider exposes /auth/login as the login URL.

    The /v1/me endpoint uses this to return 401 with a login_url
    so the frontend knows where to redirect.
    """
    config = _make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    assert provider.login_url == "/auth/login"


# ── create_auth_provider factory ─────────────────────────────────


def test_factory_returns_header_provider_explicit_zero_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit OMNIGENT_AUTH_PROVIDER=header returns a header provider.

    This is the zero-config path for Databricks Apps and any deploy
    behind a proxy that injects ``X-Forwarded-Email``. Header is the
    env-unset default, but proxy-fronted deploys set the value
    explicitly so an ambient ``OMNIGENT_AUTH_ENABLED=1`` can't
    flip them into accounts (or oidc) mode.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    provider = create_auth_provider()
    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._source == "header"
    assert provider.login_url is None


def test_factory_returns_header_provider_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit OMNIGENT_AUTH_PROVIDER=header returns a header provider."""
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    provider = create_auth_provider()
    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._source == "header"


def test_factory_rejects_unknown_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown provider value raises RuntimeError at startup.

    This is the fail-loud behavior: a typo in the env var
    surfaces immediately, not at the first request.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "saml")
    with pytest.raises(RuntimeError, match="Unknown OMNIGENT_AUTH_PROVIDER"):
        create_auth_provider()


def test_factory_oidc_fails_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OIDC provider fails at startup when required env vars are missing.

    This is the fail-loud startup validation: missing OIDC config
    surfaces as a clear error before the server accepts connections.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "oidc")
    # Don't set any OMNIGENT_OIDC_* vars.
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OMNIGENT_OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("OMNIGENT_OIDC_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("OMNIGENT_OIDC_REDIRECT_URI", raising=False)
    monkeypatch.delenv("OMNIGENT_OIDC_COOKIE_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variable"):
        create_auth_provider()


# ── OIDCConfig.from_env validation ───────────────────────────────


def test_oidc_config_rejects_short_cookie_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cookie secret shorter than 32 bytes is rejected at startup.

    A short secret weakens the HMAC-SHA256 signing and makes
    brute-force attacks feasible.
    """
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "https://app/callback")
    # 16 bytes = 32 hex chars, but we need 32 bytes = 64 hex chars.
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 16)
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        OIDCConfig.from_env()


def test_oidc_config_rejects_invalid_hex_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-hex cookie secret is rejected at startup.

    The secret must be hex-encoded so it can be safely stored in
    environment variables.
    """
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "https://app/callback")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "not-hex-at-all")
    with pytest.raises(RuntimeError, match="valid hex string"):
        OIDCConfig.from_env()


def test_oidc_config_github_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub issuer produces a github provider type with hardcoded endpoints.

    GitHub doesn't implement OIDC discovery, so we hardcode the
    endpoints. If this test fails, the GitHub login flow will break.
    """
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test-client")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "https://app/callback")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 32)

    config = OIDCConfig.from_env()

    assert config.provider_type == "github"
    assert config.authorization_endpoint == "https://github.com/login/oauth/authorize"
    assert config.token_endpoint == "https://github.com/login/oauth/access_token"
    assert config.jwks_uri is None, "GitHub has no JWKS URI (no id_token)."
    assert "read:user" in config.scopes
    assert "user:email" in config.scopes


def test_oidc_config_github_empty_scopes_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty OMNIGENT_OIDC_SCOPES → provider default (else GitHub 404s consent)."""
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "https://app/callback")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 32)
    monkeypatch.setenv("OMNIGENT_OIDC_SCOPES", "")

    config = OIDCConfig.from_env()

    assert "read:user" in config.scopes
    assert "user:email" in config.scopes


def test_oidc_config_allowed_domains_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMNIGENT_OIDC_ALLOWED_DOMAINS is parsed into a frozenset.

    Domains are lowercased and trimmed. If parsing is broken,
    the domain allowlist check in the callback would accept or
    reject the wrong domains.
    """
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "https://app/callback")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 32)
    monkeypatch.setenv("OMNIGENT_OIDC_ALLOWED_DOMAINS", " Example.COM , test.org ")

    config = OIDCConfig.from_env()

    assert config.allowed_domains == frozenset({"example.com", "test.org"})


def test_oidc_redirect_uri_derived_from_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """With REDIRECT_URI unset, it's derived as https://<OMNIGENT_DOMAIN>/auth/callback.

    Lets a domain-based deploy set one var (OMNIGENT_DOMAIN, already
    needed by the Caddy overlay) instead of two, and removes the
    http/https scheme mistake. GitHub branch → no network discovery.
    """
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "test")
    monkeypatch.delenv("OMNIGENT_OIDC_REDIRECT_URI", raising=False)
    monkeypatch.setenv("OMNIGENT_DOMAIN", "omnigent.example.com")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 32)

    config = OIDCConfig.from_env()

    assert config.redirect_uri == "https://omnigent.example.com/auth/callback"
    # Derived URI is https → secure cookies + __Host- prefix.
    assert config.secure_cookies is True


def test_oidc_explicit_redirect_uri_wins_over_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit REDIRECT_URI is used verbatim even when OMNIGENT_DOMAIN is set."""
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "http://10.0.0.5:8000/auth/callback")
    monkeypatch.setenv("OMNIGENT_DOMAIN", "omnigent.example.com")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 32)

    config = OIDCConfig.from_env()

    assert config.redirect_uri == "http://10.0.0.5:8000/auth/callback"


def test_oidc_redirect_uri_required_without_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loud when neither REDIRECT_URI nor OMNIGENT_DOMAIN is set."""
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "test")
    monkeypatch.delenv("OMNIGENT_OIDC_REDIRECT_URI", raising=False)
    monkeypatch.delenv("OMNIGENT_DOMAIN", raising=False)
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 32)

    with pytest.raises(RuntimeError, match="OMNIGENT_OIDC_REDIRECT_URI"):
        OIDCConfig.from_env()
