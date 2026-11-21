"""JWT bearer authorization grant (RFC 7523 §2.1) for externally issued machine identities.

A headless client that already holds an access token from a trusted identity
provider (for example a Keycloak or Entra service-account token obtained with
that provider's own client-credentials grant) exchanges it at ``POST
/oauth/token`` for an Omnigent access token acting as a configured machine
principal. The identity provider owns the client secret and its revocation:
disabling the client there stops new exchanges, and every issued token expires
no later than the assertion it was exchanged for.

This module owns no route. It builds the
``urn:ietf:params:oauth:grant-type:jwt-bearer`` branch of the app's one ``POST
/oauth/token``, which
:func:`omnigent.server.routes.device_auth.create_oauth_token_router` dispatches
by ``grant_type``.

**Opt-in and default-off**: with no ``OMNIGENT_JWT_BEARER_*`` variable set the
handler is never built and the grant answers ``unsupported_grant_type``.

- ``OMNIGENT_JWT_BEARER_ISSUER`` — the exact ``iss`` of accepted assertions.
- ``OMNIGENT_JWT_BEARER_AUDIENCE`` — the ``aud`` assertions must carry, naming
  this server, so a token minted for another relying party is never accepted.
- ``OMNIGENT_JWT_BEARER_CLIENTS`` — comma-separated ``client=principal`` pairs.
  The client is read from the assertion's client claim; only listed clients
  are accepted, and each acts as its own configured principal. Claims the
  identity provider lets a client or user choose (``email``, ``sub``) never
  name the principal.
- ``OMNIGENT_JWT_BEARER_CLIENT_CLAIM`` — the claim naming the client; default
  ``azp`` (``client_id`` for RFC 9068 access tokens).
- ``OMNIGENT_JWT_BEARER_JWKS_URI`` — the issuer's signing keys. Defaults to the
  login OIDC provider's JWKS when the issuer is the login issuer.
- ``OMNIGENT_JWT_BEARER_TOKEN_TTL`` — maximum issued token lifetime in seconds
  (default and ceiling 3600).

The issued token is the same delegated shape the client-credentials grant
mints: ``scope`` set, no ``grant_id``, ``act.client_id`` naming the client. The
auth layer therefore confines it to the delegated path allowlist and never
caches it. That allowlist confines the path, not the privilege within it, so
every principal is vetted as non-admin at mount and again on every exchange.

See ``designs/JWT_BEARER_GRANT.md`` for the design and threat model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import jwt
from fastapi import Request
from starlette.datastructures import FormData
from starlette.responses import JSONResponse, Response

from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC, UnifiedAuthProvider
from omnigent.server.routes._oauth import (
    NO_STORE_HEADERS,
    RATE_LIMITER_MAX_KEYS,
    SlidingWindowRateLimiter,
    oauth_error,
)
from omnigent.server.routes.device_auth import DELEGATED_SCOPE, mint_delegated_token

if TYPE_CHECKING:
    from omnigent.server.admin_list import AdminList
    from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

JWT_BEARER_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

_ISSUER_ENV = "OMNIGENT_JWT_BEARER_ISSUER"
_AUDIENCE_ENV = "OMNIGENT_JWT_BEARER_AUDIENCE"
_CLIENTS_ENV = "OMNIGENT_JWT_BEARER_CLIENTS"
_CLIENT_CLAIM_ENV = "OMNIGENT_JWT_BEARER_CLIENT_CLAIM"
_JWKS_URI_ENV = "OMNIGENT_JWT_BEARER_JWKS_URI"
_TOKEN_TTL_ENV = "OMNIGENT_JWT_BEARER_TOKEN_TTL"
_ALL_ENV = (
    _ISSUER_ENV,
    _AUDIENCE_ENV,
    _CLIENTS_ENV,
    _CLIENT_CLAIM_ENV,
    _JWKS_URI_ENV,
    _TOKEN_TTL_ENV,
)

# Asymmetric only: with an HS* algorithm the published public key would be
# usable as the HMAC secret.
_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
_CLOCK_SKEW_SECONDS = 30
_MAX_TOKEN_TTL_SECONDS = 3600
# An assertion is replayable until it expires, so a long-lived one would let a
# leaked token keep minting long after the client was disabled.
_MAX_ASSERTION_LIFETIME_SECONDS = 3600
# Unknown key ids refetch the key set at most this often, so a stream of forged
# assertions cannot turn the grant into a fetch amplifier against the issuer.
_JWKS_UNKNOWN_KID_REFRESH_SECONDS = 30
_JWKS_TIMEOUT_SECONDS = 5

_TOKEN_RATE_MAX = 10
_TOKEN_RATE_WINDOW_SECONDS = 60
_ACCOUNT_SCAN_LIMIT = 1000


def _token_ttl_from_env() -> int:
    raw = os.environ.get(_TOKEN_TTL_ENV, "").strip()
    if not raw:
        return _MAX_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"jwt-bearer: {_TOKEN_TTL_ENV}={raw!r} is not an integer") from exc
    if not 0 < ttl <= _MAX_TOKEN_TTL_SECONDS:
        raise RuntimeError(
            f"jwt-bearer: {_TOKEN_TTL_ENV}={raw!r} must be between 1 and "
            f"{_MAX_TOKEN_TTL_SECONDS} seconds"
        )
    return ttl


def _parse_clients(raw: str) -> dict[str, str]:
    clients: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        client, sep, principal = entry.partition("=")
        client, principal = client.strip(), principal.strip()
        if not sep or not client or not principal:
            raise RuntimeError(
                f"jwt-bearer: {_CLIENTS_ENV} entry {entry!r} is not a client=principal pair"
            )
        if client in clients:
            raise RuntimeError(f"jwt-bearer: {_CLIENTS_ENV} lists client {client!r} twice")
        if principal in (RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC):
            raise RuntimeError(
                f"jwt-bearer: {_CLIENTS_ENV} maps {client!r} to the reserved identity "
                f"{principal!r}; use a dedicated principal"
            )
        clients[client] = principal
    if not clients:
        raise RuntimeError(f"jwt-bearer: {_CLIENTS_ENV} names no client=principal pair")
    return clients


@dataclass(frozen=True)
class JwtBearerConfig:
    """The trusted issuer and its accepted clients, read from the environment.

    :param issuer: Exact ``iss`` of accepted assertions, e.g.
        ``"https://sso.example.com/realms/main"``.
    :param audience: Required ``aud``, e.g. ``"omnigent-api"``.
    :param clients: Accepted client → the principal it acts as, e.g.
        ``{"ci-triage": "ci-triage@ci.invalid"}``.
    :param client_claim: Assertion claim naming the client, e.g. ``"azp"``.
    :param jwks_uri: Explicit JWKS URL, or ``None`` to use the login issuer's.
    :param token_ttl_seconds: Ceiling on the issued token lifetime.
    """

    issuer: str
    audience: str
    clients: dict[str, str]
    client_claim: str
    jwks_uri: str | None
    token_ttl_seconds: int

    @staticmethod
    def from_env() -> JwtBearerConfig | None:
        """Build the config, or ``None`` when every variable is unset.

        :raises RuntimeError: On a partial or malformed configuration, so a
            deploy that meant to enable the grant cannot start without it.
        """
        if not any(os.environ.get(name, "").strip() for name in _ALL_ENV):
            return None
        issuer = os.environ.get(_ISSUER_ENV, "").strip()
        audience = os.environ.get(_AUDIENCE_ENV, "").strip()
        raw_clients = os.environ.get(_CLIENTS_ENV, "").strip()
        if not (issuer and audience and raw_clients):
            raise RuntimeError(
                f"jwt-bearer: {_ISSUER_ENV}, {_AUDIENCE_ENV} and {_CLIENTS_ENV} must all "
                "be set to enable the grant, or every OMNIGENT_JWT_BEARER_* variable "
                "unset to leave it off"
            )
        return JwtBearerConfig(
            issuer=issuer,
            audience=audience,
            clients=_parse_clients(raw_clients),
            client_claim=os.environ.get(_CLIENT_CLAIM_ENV, "").strip() or "azp",
            jwks_uri=os.environ.get(_JWKS_URI_ENV, "").strip() or None,
            token_ttl_seconds=_token_ttl_from_env(),
        )


class _SigningKeys:
    """The issuer's JWKS, with unknown-``kid`` refreshes rate-limited."""

    def __init__(
        self,
        client: jwt.PyJWKClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._last_forced_refresh: float | None = None

    def key_for(self, kid: str) -> jwt.PyJWK | None:
        key = self._client.match_kid(self._client.get_signing_keys(), kid)
        if key is not None:
            return key
        with self._lock:
            now = self._clock()
            if (
                self._last_forced_refresh is not None
                and now - self._last_forced_refresh < _JWKS_UNKNOWN_KID_REFRESH_SECONDS
            ):
                return None
            self._last_forced_refresh = now
        return self._client.match_kid(self._client.get_signing_keys(refresh=True), kid)


class _Rejected(Exception):
    """An assertion that fails verification; the message is logged, not returned."""


@dataclass(frozen=True)
class _Exchange:
    client: str
    principal: str
    expires_at: float
    assertion_jti: str | None


def _verify_assertion(assertion: str, config: JwtBearerConfig, keys: _SigningKeys) -> _Exchange:
    try:
        header = jwt.get_unverified_header(assertion)
        unverified = jwt.decode(assertion, options={"verify_signature": False})
    except jwt.InvalidTokenError as exc:
        raise _Rejected(f"malformed assertion: {exc}") from exc
    alg, kid = header.get("alg"), header.get("kid")
    if alg not in _ALGORITHMS:
        raise _Rejected(f"algorithm {alg!r} is not an accepted asymmetric algorithm")
    if unverified.get("iss") != config.issuer:
        raise _Rejected("untrusted issuer")
    if not isinstance(kid, str) or not kid:
        raise _Rejected("assertion names no signing key")
    try:
        key = keys.key_for(kid)
    except jwt.PyJWKClientError as exc:
        raise _Rejected(f"signing keys unavailable: {exc}") from exc
    if key is None:
        raise _Rejected(f"issuer publishes no signing key {kid!r}")
    if key.algorithm_name and key.algorithm_name != alg:
        raise _Rejected(f"key {kid!r} is {key.algorithm_name}, assertion says {alg}")
    try:
        claims = jwt.decode(
            assertion,
            key.key,
            algorithms=[alg],
            audience=config.audience,
            issuer=config.issuer,
            leeway=_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.InvalidTokenError as exc:
        raise _Rejected(str(exc)) from exc
    exp, iat = claims["exp"], claims["iat"]
    if not isinstance(exp, int | float) or not isinstance(iat, int | float):
        raise _Rejected("exp and iat must be numeric")
    if exp - iat > _MAX_ASSERTION_LIFETIME_SECONDS:
        raise _Rejected(
            f"assertion lifetime {int(exp - iat)}s exceeds {_MAX_ASSERTION_LIFETIME_SECONDS}s"
        )
    client = claims.get(config.client_claim)
    principal = config.clients.get(client) if isinstance(client, str) else None
    if not isinstance(client, str) or principal is None:
        raise _Rejected(f"{config.client_claim}={client!r} is not an accepted client")
    jti = claims.get("jti")
    return _Exchange(
        client=client,
        principal=principal,
        expires_at=float(exp),
        assertion_jti=jti if isinstance(jti, str) else None,
    )


class _Verdict(str, Enum):
    OK = "ok"
    ADMIN = "admin"
    UNVERIFIABLE = "unverifiable"


def _vet_principal(is_admin: Callable[[str], bool] | None, principal: str) -> _Verdict:
    """Refuse a principal the ``is_admin`` → OWNER override would reach.

    Consults the admin list as well as the stored flag: a listed identity is
    promoted on its first identity lookup, so it is an admin in waiting.
    """
    if is_admin is None:
        return _Verdict.OK
    try:
        return _Verdict.ADMIN if is_admin(principal) else _Verdict.OK
    except Exception:
        _logger.exception("jwt-bearer: could not determine whether %r is an admin", principal)
        return _Verdict.UNVERIFIABLE


def _warn_on_shared_identities(
    config: JwtBearerConfig,
    permission_store: PermissionStore | None,
    allowed_login_domains: frozenset[str] | None,
) -> None:
    principals = set(config.clients.values())
    for principal in sorted(principals):
        domain = principal.rpartition("@")[2].lower()
        if allowed_login_domains and domain in allowed_login_domains:
            _logger.warning(
                "jwt-bearer: principal %r is in an allowed login domain, so a person "
                "signing in with that address would share its identity",
                principal,
            )
    if permission_store is None:
        return
    try:
        accounts = permission_store.list_users(limit=_ACCOUNT_SCAN_LIMIT)
    except Exception:  # noqa: BLE001 (advisory scan, must never gate the mount)
        _logger.debug("jwt-bearer: could not scan accounts for shared identities", exc_info=True)
        return
    for account in accounts:
        if account.id in principals:
            _logger.warning(
                "jwt-bearer: principal %r is an existing account; exchanged tokens act "
                "as that identity. Map clients to dedicated principals.",
                account.id,
            )


def create_jwt_bearer_handler(
    auth_provider: UnifiedAuthProvider,
    permission_store: PermissionStore | None,
    *,
    admin_list: AdminList | None = None,
    jwks_client: jwt.PyJWKClient | None = None,
) -> Callable[[Request, FormData], Awaitable[Response]] | None:
    """Build the ``jwt-bearer`` branch of ``POST /oauth/token``.

    Pass the result to
    :func:`omnigent.server.routes.device_auth.create_oauth_token_router` as
    ``handle_jwt_bearer``.

    :param auth_provider: The active provider; must be ``oidc`` or ``accounts``
        mode, whose cookie secret signs the issued token.
    :param permission_store: Used to refuse admin principals and to warn when a
        principal is an existing account. ``None`` when the deploy has none.
    :param admin_list: The admin roster, also consulted when vetting.
    :param jwks_client: Key client override; built from the config when omitted.
    :returns: The handler, or ``None`` when the grant is not configured.
    :raises RuntimeError: On a non-cookie auth mode or a misconfiguration.
    """
    config = JwtBearerConfig.from_env()
    if config is None:
        return None
    if auth_provider._source not in ("oidc", "accounts"):
        raise RuntimeError(
            f"jwt-bearer: the grant needs oidc or accounts auth, not {auth_provider._source!r}"
        )
    oidc_config = auth_provider._oidc_config if auth_provider._source == "oidc" else None
    cookie_config = oidc_config if oidc_config is not None else auth_provider._accounts_config
    if cookie_config is None:
        raise RuntimeError(f"jwt-bearer: {auth_provider._source!r} auth has no cookie config")
    cookie_secret = cookie_config.cookie_secret
    provider_name = auth_provider._source

    jwks_uri = config.jwks_uri
    if jwks_uri is None and oidc_config is not None and oidc_config.issuer == config.issuer:
        jwks_uri = oidc_config.jwks_uri
    if jwks_client is None:
        if not jwks_uri:
            raise RuntimeError(
                f"jwt-bearer: set {_JWKS_URI_ENV}; the issuer {config.issuer!r} is not the "
                "login OIDC issuer, so its signing keys are unknown"
            )
        jwks_client = jwt.PyJWKClient(jwks_uri, timeout=_JWKS_TIMEOUT_SECONDS)
    keys = _SigningKeys(jwks_client)

    def is_admin(principal: str) -> bool:
        listed = admin_list is not None and admin_list.is_admin(principal)
        return listed or (permission_store is not None and permission_store.is_admin(principal))

    admin_check = is_admin if (admin_list is not None or permission_store is not None) else None
    for client, principal in sorted(config.clients.items()):
        verdict = _vet_principal(admin_check, principal)
        if verdict is not _Verdict.OK:
            _logger.error(
                "jwt-bearer: client %r maps to %r, which is %s; its exchanges will be "
                "refused. Map it to a dedicated non-admin principal.",
                client,
                principal,
                "an admin" if verdict is _Verdict.ADMIN else "not verifiably non-admin",
            )
    if admin_check is None:
        _logger.warning(
            "jwt-bearer: no permission store or admin list, so principals cannot be vetted"
        )
    _warn_on_shared_identities(
        config, permission_store, oidc_config.allowed_domains if oidc_config else None
    )
    _logger.info(
        "jwt-bearer: grant enabled on /oauth/token for issuer=%s audience=%s clients=%s",
        config.issuer,
        config.audience,
        ",".join(sorted(config.clients)),
    )

    # Keyed by the verified client, never the source address: behind a reverse
    # proxy every caller shares one address, so a source-keyed budget would let
    # unauthenticated requests exhaust it for every legitimate client.
    rate_limiter = SlidingWindowRateLimiter(
        _TOKEN_RATE_MAX, _TOKEN_RATE_WINDOW_SECONDS, RATE_LIMITER_MAX_KEYS
    )

    async def handle_jwt_bearer(request: Request, form: FormData) -> Response:
        """Exchange a trusted issuer's assertion for a delegated access token."""
        client_ip = request.client.host if request.client else "unknown"
        assertion = str(form.get("assertion") or "")
        if not assertion:
            return oauth_error("invalid_request")
        try:
            exchange = await asyncio.to_thread(_verify_assertion, assertion, config, keys)
        except _Rejected as exc:
            _logger.info("jwt-bearer: rejected assertion from %s: %s", client_ip, exc)
            return oauth_error("invalid_grant")
        if not rate_limiter.allow(exchange.client, time.time()):
            _logger.warning("jwt-bearer: throttling client %r", exchange.client)
            return oauth_error(
                "slow_down",
                status_code=429,
                headers={"Retry-After": str(_TOKEN_RATE_WINDOW_SECONDS)},
            )

        verdict = await asyncio.to_thread(_vet_principal, admin_check, exchange.principal)
        if verdict is not _Verdict.OK:
            _logger.error(
                "jwt-bearer: refusing client %r: principal %r is %s",
                exchange.client,
                exchange.principal,
                "an admin" if verdict is _Verdict.ADMIN else "not verifiably non-admin",
            )
            return oauth_error("unauthorized_client")

        ttl = min(config.token_ttl_seconds, int(exchange.expires_at - time.time()))
        if ttl <= 0:
            return oauth_error("invalid_grant")
        jti = secrets.token_urlsafe(16)
        access_token = mint_delegated_token(
            exchange.principal,
            cookie_secret,
            ttl,
            provider_name,
            grant_id=None,
            client_id=exchange.client,
            jti=jti,
            scope=DELEGATED_SCOPE,
        )
        _logger.info(
            "jwt-bearer: issued token jti=%s for client=%s principal=%s (assertion jti=%s)",
            jti,
            exchange.client,
            exchange.principal,
            exchange.assertion_jti,
        )
        return JSONResponse(
            status_code=200,
            content={
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": ttl,
                "scope": DELEGATED_SCOPE,
            },
            headers=NO_STORE_HEADERS,
        )

    return handle_jwt_bearer
