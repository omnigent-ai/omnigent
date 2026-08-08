"""OAuth 2.0 client-credentials grant (RFC 6749 §4.4).

A single confidential client exchanges its own credentials for a delegated,
path-scoped access token acting as a fixed machine principal — no browser,
no per-user consent, no device flow. This is the grant a headless process
uses when it is its own resource owner rather than acting for a human.

Endpoint (mounted at the app root):

- ``POST /oauth/token`` — handles ONLY ``grant_type=client_credentials``.
  On success returns ``{access_token, token_type, expires_in}``. Any other
  grant type is ``unsupported_grant_type``; a bad / absent / mismatched
  client is ``401 invalid_client``; more requests from one source than the
  throttle allows is ``429 slow_down``. The client check runs before anything
  has authenticated, so the endpoint carries the same per-IP sliding-window
  limiter the device grant's public authorize endpoint uses.

**Opt-in and default-off**, like the device grant next door: the machine
client's env config *is* the opt-in, so ``POST /oauth/token`` stays unmounted
(404) in every deployment that configures none. The device grant needs its own
``OMNIGENT_DEVICE_GRANT_ENABLED`` flag because its endpoints are useful with
zero config (a public client); this grant has nothing to serve without a
configured client, so a separate flag would only be a second way to say the
same thing. Mounted in the cookie-based auth modes (``oidc`` and ``accounts``)
— it needs the HS256 ``cookie_secret`` both configs expose, which is also the
key the minted token is validated against — and never alongside the device
grant, which already claims ``POST /oauth/token`` in accounts mode.

The confidential client is a single env-configured registry entry — no
database, no migration:

- ``OMNIGENT_MACHINE_CLIENT_ID`` — the client identifier.
- ``OMNIGENT_MACHINE_CLIENT_SECRET_HASH`` — the client secret, stored only as
  its :func:`hash_secret` digest (HMAC-SHA256 keyed by ``cookie_secret``),
  never the raw secret. Must have that digest's shape (64 hex characters) and
  is verified in constant time. Only the digest is configured, so the server
  cannot measure the secret's entropy — generate it with
  ``secrets.token_urlsafe(32)`` or equivalent. The throttle below bounds how
  fast a secret can be guessed; its entropy is what makes guessing hopeless.
- ``OMNIGENT_MACHINE_SUB`` — the machine principal the minted token acts as.
- ``OMNIGENT_MACHINE_TOKEN_TTL`` — access-token lifetime in seconds (default
  3600, and capped there: expiry is this model's only revocation, so the TTL
  is what bounds a stolen token). The client re-mints rather than refreshing —
  there is no refresh token and no store-backed per-token revocation (that is
  the store-backed delegated grant's job).

All three of the first group must be set to enable the grant, or all unset to
leave it off; any other combination — a malformed secret hash, an unusable
TTL — is an operator error and refuses to start rather than coming up with a
token endpoint that answers every request ``invalid_request``, which reads as
a client bug.

The minted token reuses the delegated JWT shape
(:func:`omnigent.server.routes.device_auth.mint_delegated_token`) with the
``scope`` claim set and no ``grant_id``: the auth layer confines it to the
delegated path allowlist and, seeing no ``grant_id``, skips the
revocation-denylist lookup.

That allowlist is a PATH confinement only. It does NOT limit the token's
privilege within an allowlisted path: the ``is_admin → LEVEL_OWNER`` override
inside /v1/sessions keys off the token's identity, so an admin ``sub`` would
own every tenant's session despite the allowlist. The machine ``sub`` must
therefore be a distinct, non-admin principal — vetted at mount and again on
every mint, so promoting it to admin later stops new tokens instead of waiting
for a restart.

See ``designs/CLIENT_CREDENTIALS.md`` for the full design + threat model.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import unquote_plus

from fastapi import APIRouter, Request
from starlette.datastructures import FormData
from starlette.responses import JSONResponse, Response

from omnigent.server.auth import (
    RESERVED_USER_LOCAL,
    RESERVED_USER_PUBLIC,
    UnifiedAuthProvider,
)
from omnigent.server.device_grant_store import hash_secret
from omnigent.server.routes._oauth import (
    NO_STORE_HEADERS,
    RATE_LIMITER_MAX_KEYS,
    SlidingWindowRateLimiter,
    oauth_error,
)
from omnigent.server.routes.device_auth import mint_delegated_token
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

_CLIENT_CREDENTIALS_GRANT = "client_credentials"
_DEFAULT_TOKEN_TTL_SECONDS = 3600
# Hard ceiling on the configured TTL. Expiry is this model's only revocation
# (no refresh token, no per-token denylist), so a long-lived token would leave
# theft unbounded. Matches the device grant's fixed access-token lifetime.
_MAX_TOKEN_TTL_SECONDS = 3600
# The stored secret's shape — :func:`hash_secret` is HMAC-SHA256, hex-encoded.
# Anchored in the pattern, so the strictness survives a caller that reaches for
# ``match`` or ``search`` instead of ``fullmatch``. ``\Z`` rather than ``$``:
# ``$`` would also accept a trailing newline.
_SECRET_HASH_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
# Protection space named by the WWW-Authenticate challenge on a 401.
_TOKEN_ENDPOINT_REALM = "omnigent"

# ── Abuse control on the unauthenticated client check ─────────────
# /oauth/token answers before anything has authenticated, so the client-secret
# comparison is reachable by anyone who can reach the port — the same exposure
# the device grant throttles on its public authorize endpoint. Only the secret's
# digest is configured, so the server cannot require entropy of the secret
# itself; a coarse per-IP sliding window over the endpoint is what bounds the
# guess rate. The ceiling sits far above honest use: a machine client mints once
# per token TTL, not once per request.
_TOKEN_RATE_MAX = 10  # max token requests…
_TOKEN_RATE_WINDOW_SECONDS = 60  # …per client IP per this window.

_CLIENT_ID_ENV = "OMNIGENT_MACHINE_CLIENT_ID"
_CLIENT_SECRET_HASH_ENV = "OMNIGENT_MACHINE_CLIENT_SECRET_HASH"
_SUB_ENV = "OMNIGENT_MACHINE_SUB"
_TOKEN_TTL_ENV = "OMNIGENT_MACHINE_TOKEN_TTL"


def _token_ttl_from_env() -> int:
    """Read the configured access-token TTL in seconds.

    :returns: The configured TTL, or :data:`_DEFAULT_TOKEN_TTL_SECONDS`.
    :raises RuntimeError: If the value is not an integer, is not positive, or
        exceeds :data:`_MAX_TOKEN_TTL_SECONDS`.
    """
    raw_ttl = os.environ.get(_TOKEN_TTL_ENV, "").strip()
    if not raw_ttl:
        return _DEFAULT_TOKEN_TTL_SECONDS
    try:
        token_ttl = int(raw_ttl)
    except ValueError as exc:
        raise RuntimeError(
            f"client-credentials: {_TOKEN_TTL_ENV}={raw_ttl!r} is not an integer number of seconds"
        ) from exc
    if token_ttl <= 0:
        raise RuntimeError(
            f"client-credentials: {_TOKEN_TTL_ENV}={raw_ttl!r} must be a positive "
            "number of seconds"
        )
    if token_ttl > _MAX_TOKEN_TTL_SECONDS:
        raise RuntimeError(
            f"client-credentials: {_TOKEN_TTL_ENV}={raw_ttl!r} exceeds the "
            f"{_MAX_TOKEN_TTL_SECONDS}s ceiling — expiry is this grant's only "
            "revocation, so the TTL is what bounds a stolen token"
        )
    return token_ttl


@dataclass(frozen=True)
class MachineClientConfig:
    """The single confidential machine client, read from the environment.

    :param client_id: The client identifier presented at the token endpoint.
    :param secret_hash: The client secret's :func:`hash_secret` digest — the
        stored form, never the raw secret.
    :param sub: The machine principal the minted token acts as (``sub``).
    :param token_ttl_seconds: Minted access-token lifetime in seconds.
    """

    client_id: str
    secret_hash: str
    sub: str
    token_ttl_seconds: int

    @staticmethod
    def from_env() -> MachineClientConfig | None:
        """Build the machine-client config, or ``None`` when unconfigured.

        Every ``OMNIGENT_MACHINE_*`` variable unset is the one clean "off", and
        is what leaves the token endpoint unmounted. Any other unusable
        combination is an operator error and raises, so a deploy that meant to
        turn machine auth on cannot come up silently without it.

        :returns: The configured client, or ``None`` when the grant is off.
        :raises RuntimeError: On a partial config, a secret hash that is not a
            keyed SHA-256 digest, a reserved principal, or a token TTL that is
            not a positive integer within the allowed ceiling.
        """
        client_id = os.environ.get(_CLIENT_ID_ENV, "").strip()
        secret_hash = os.environ.get(_CLIENT_SECRET_HASH_ENV, "").strip()
        sub = os.environ.get(_SUB_ENV, "").strip()
        if not (client_id or secret_hash or sub):
            return None
        if not (client_id and secret_hash and sub):
            raise RuntimeError(
                f"client-credentials: {_CLIENT_ID_ENV}, {_CLIENT_SECRET_HASH_ENV} "
                f"and {_SUB_ENV} must all be set to enable the grant, or all be "
                "unset to leave it off"
            )
        # Catches the raw secret pasted where its digest belongs. Unchecked,
        # that config would simply never match: a token endpoint that 401s
        # every correct credential, with nothing in the log to say why.
        if not _SECRET_HASH_RE.fullmatch(secret_hash):
            raise RuntimeError(
                f"client-credentials: {_CLIENT_SECRET_HASH_ENV} must be the client "
                "secret's 64-character hex hash_secret digest, not the raw secret "
                f"(got {len(secret_hash)} characters)"
            )
        # The machine principal must be a real, distinct identity — the
        # reserved sentinels resolve to no account the grant could scope to.
        if sub in (RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC):
            raise RuntimeError(
                f"client-credentials: {_SUB_ENV}={sub!r} is a reserved identity; "
                "point it at a distinct, dedicated principal"
            )

        return MachineClientConfig(
            client_id=client_id,
            # hash_secret emits lowercase and the comparison is on the exact
            # string, so an uppercased digest would silently never match.
            secret_hash=secret_hash.lower(),
            sub=sub,
            token_ttl_seconds=_token_ttl_from_env(),
        )


def _presented_client(request: Request, form: FormData) -> tuple[str, str] | None:
    """Resolve the presented ``(client_id, client_secret)`` pair, or ``None``.

    RFC 6749 §2.3.1: a confidential client may authenticate with HTTP Basic
    (``Authorization: Basic base64(client_id:client_secret)``) or with
    ``client_id`` / ``client_secret`` form fields. Basic takes precedence
    when present. Returns ``None`` when neither carries a usable pair, which
    the caller maps to ``invalid_client``.

    Both halves of a Basic credential are ``application/x-www-form-urlencoded``
    before the base64, so both are decoded after the split on ``":"`` — a
    secret containing ``":"``, ``"%"``, ``"+"`` or a space is otherwise read
    wrong. Form fields need no such step: the form parser already decoded them.
    """
    scheme, _, param = request.headers.get("Authorization", "").partition(" ")
    # RFC 7235 §2.1: auth schemes are matched case-insensitively.
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(param.strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        client_id, sep, secret = decoded.partition(":")
        if not sep:
            return None
        return (unquote_plus(client_id), unquote_plus(secret))
    client_id = str(form.get("client_id") or "")
    secret = str(form.get("client_secret") or "")
    if client_id and secret:
        return (client_id, secret)
    return None


def _client_matches(
    client_id: str,
    secret: str,
    config: MachineClientConfig,
    cookie_secret: bytes,
) -> bool:
    """Constant-time check of a presented client id + secret against config.

    The secret is compared as its :func:`hash_secret` digest (the raw secret
    is never stored). Both the id and the secret-digest comparisons run and
    are combined without short-circuiting, so a mismatch reveals nothing
    through timing about which half was wrong.
    """
    id_ok = hmac.compare_digest(client_id.encode("utf-8"), config.client_id.encode("utf-8"))
    presented_hash = hash_secret(secret, cookie_secret)
    secret_ok = hmac.compare_digest(
        presented_hash.encode("utf-8"), config.secret_hash.encode("utf-8")
    )
    return id_ok and secret_ok


def _invalid_client(request: Request) -> JSONResponse:
    """Build the ``401 invalid_client`` answer to a failed client authentication.

    RFC 6749 §5.2: when the client authenticated through the ``Authorization``
    header the 401 must carry a ``WWW-Authenticate`` naming the scheme it used.
    A client that sent form credentials gets no challenge — it has no header
    attempt to retry.
    """
    headers = {}
    if request.headers.get("Authorization"):
        headers["WWW-Authenticate"] = f'Basic realm="{_TOKEN_ENDPOINT_REALM}", charset="UTF-8"'
    return oauth_error("invalid_client", status_code=401, headers=headers)


class _SubVerdict(str, Enum):
    """Outcome of vetting the machine principal against the permission store.

    ``ADMIN`` and ``UNVERIFIABLE`` both refuse the grant, but they are separate
    values so a caller's log can name the cause it actually hit: a store that
    could not answer is not an admin principal, and reporting it as one sends
    the operator to audit config that is fine.
    """

    OK = "ok"
    ADMIN = "admin"
    UNVERIFIABLE = "unverifiable"


def _vet_machine_sub(permission_store: PermissionStore | None, sub: str) -> _SubVerdict:
    """Vet the machine *sub* against the ``is_admin`` → OWNER override.

    The path allowlist confines a machine token to the session APIs but does
    not limit its privilege there: /v1/sessions grants ``LEVEL_OWNER`` to any
    ``is_admin`` identity, so an admin ``sub`` would own every tenant's
    session. Enabling such a client is a misconfiguration, checked at mount and
    again before every mint.

    :param permission_store: The permission store, or ``None`` when the deploy
        has none — the store-backed override cannot fire then, so the sub is
        ``OK``.
    :param sub: The configured machine principal.
    :returns: ``OK`` when the sub may be granted, ``ADMIN`` when it inherits
        the override, ``UNVERIFIABLE`` when the store could not answer. The
        last fails closed: a machine client we cannot vet gets no token.
    """
    if permission_store is None:
        return _SubVerdict.OK
    try:
        is_admin = permission_store.is_admin(sub)
    except Exception:
        _logger.exception(
            "client-credentials: the permission store could not answer whether %s=%r is an admin",
            _SUB_ENV,
            sub,
        )
        return _SubVerdict.UNVERIFIABLE
    return _SubVerdict.ADMIN if is_admin else _SubVerdict.OK


def create_client_credentials_router(
    auth_provider: UnifiedAuthProvider,
    permission_store: PermissionStore | None,
) -> APIRouter | None:
    """Build the ``POST /oauth/token`` client-credentials router.

    :param auth_provider: The active provider. Must be a cookie-based mode
        (``oidc`` or ``accounts``); its cookie config supplies the HS256
        signing key.
    :param permission_store: The session-permission store, used to refuse an
        admin ``sub`` (see :func:`_vet_machine_sub`). ``None`` when the deploy
        has no store — the store-backed OWNER override cannot fire then.
    :returns: An ``APIRouter`` to mount at the app root, or ``None`` when no
        machine client is configured (the grant's default-off state) or the
        configured principal is refused. Either way ``/oauth/token`` is left
        unrouted rather than answering as permanently broken.
    :raises RuntimeError: If *auth_provider* is not a cookie-based mode or
        exposes no cookie secret, or the machine client is misconfigured.
    """
    if auth_provider._source not in ("oidc", "accounts"):
        raise RuntimeError(
            "create_client_credentials_router requires oidc or accounts auth "
            f"(got {auth_provider._source!r})"
        )
    cookie_config = (
        auth_provider._oidc_config
        if auth_provider._source == "oidc"
        else auth_provider._accounts_config
    )
    if cookie_config is None:
        raise RuntimeError(
            "create_client_credentials_router needs the HS256 cookie secret, but "
            f"{auth_provider._source!r} auth carries no cookie config"
        )
    cookie_secret = cookie_config.cookie_secret
    provider_name = auth_provider._source

    config = MachineClientConfig.from_env()
    if config is None:
        _logger.debug(
            "client-credentials: no machine client configured (%s unset); "
            "/oauth/token stays unmounted",
            _CLIENT_ID_ENV,
        )
        return None
    # Vetting the principal needs a live store, so an unsuitable or
    # unverifiable sub leaves the grant unmounted rather than refusing to
    # start — unlike a bad config, which raises above.
    verdict = _vet_machine_sub(permission_store, config.sub)
    if verdict is _SubVerdict.ADMIN:
        _logger.error(
            "client-credentials: %s=%r is an admin principal; refusing to mount "
            "the machine grant. The path allowlist does NOT cover the "
            "is_admin→OWNER override in /v1/sessions, so an admin machine client "
            "would own every session. Point %s at a distinct, non-admin identity.",
            _SUB_ENV,
            config.sub,
            _SUB_ENV,
        )
        return None
    if verdict is _SubVerdict.UNVERIFIABLE:
        _logger.error(
            "client-credentials: the permission store could not say whether %s=%r "
            "is an admin (traceback above); refusing to mount the machine grant "
            "rather than enable an unvetted principal. This is a store fault, not "
            "necessarily a bad %s — retry once the store answers.",
            _SUB_ENV,
            config.sub,
            _SUB_ENV,
        )
        return None
    if permission_store is None:
        _logger.warning(
            "client-credentials: no permission store wired — the admin-sub guard "
            "could not run for %s=%r (the store-backed OWNER override is inert "
            "without a store)",
            _SUB_ENV,
            config.sub,
        )
    _logger.info(
        "client-credentials: /oauth/token enabled for client_id=%s (sub=%s); "
        "cookie_secret keys both %s verification and token signing, so rotating "
        "it invalidates the stored hash and every issued token",
        config.client_id,
        config.sub,
        _CLIENT_SECRET_HASH_ENV,
    )

    router = APIRouter()
    _rate_limiter = SlidingWindowRateLimiter(
        _TOKEN_RATE_MAX, _TOKEN_RATE_WINDOW_SECONDS, RATE_LIMITER_MAX_KEYS
    )

    @router.post("/oauth/token")
    async def token(request: Request) -> Response:
        """Exchange machine client credentials for a delegated token.

        Handles only ``grant_type=client_credentials``; any other grant is
        ``unsupported_grant_type``. The principal is re-vetted once the client
        has authenticated, so promoting the machine ``sub`` to admin stops new
        tokens without a restart (issued ones are bounded by the TTL).

        Throttled per client IP ahead of the credential comparison, so the
        secret cannot be guessed at line rate.
        """
        # Ahead of everything else, including the grant_type check: a limiter
        # that only counted failed authentications would still answer the guess
        # that happened to be right, so the guess RATE is what has to be
        # bounded. ``slow_down`` + 429 is the shape the device grant's throttle
        # already answers with (RFC 8628 §3.5).
        client_ip = request.client.host if request.client else "unknown"
        if not _rate_limiter.allow(client_ip, time.time()):
            return oauth_error("slow_down", status_code=429)

        form = await request.form()
        grant_type = str(form.get("grant_type") or "")
        if grant_type != _CLIENT_CREDENTIALS_GRANT:
            return oauth_error("unsupported_grant_type")

        presented = _presented_client(request, form)
        if presented is None:
            return _invalid_client(request)
        client_id, secret = presented
        if not _client_matches(client_id, secret, config, cookie_secret):
            return _invalid_client(request)

        verdict = _vet_machine_sub(permission_store, config.sub)
        if verdict is _SubVerdict.ADMIN:
            _logger.error(
                "oauth/token: refusing to mint for %s=%r — the principal is now an "
                "admin, and an admin machine token would own every session",
                _SUB_ENV,
                config.sub,
            )
            return oauth_error("unauthorized_client", status_code=403)
        if verdict is _SubVerdict.UNVERIFIABLE:
            _logger.error(
                "oauth/token: refusing to mint for %s=%r — the permission store "
                "could not say whether the principal is an admin (traceback "
                "above), so the grant fails closed until it answers",
                _SUB_ENV,
                config.sub,
            )
            return oauth_error("unauthorized_client", status_code=403)

        access_token = mint_delegated_token(
            config.sub,
            cookie_secret,
            config.token_ttl_seconds,
            provider_name,
            client_id=config.client_id,
            jti=secrets.token_urlsafe(16),
        )
        _logger.info(
            "oauth/token: issued client-credentials token for client_id=%s (sub=%s)",
            config.client_id,
            config.sub,
        )
        return JSONResponse(
            status_code=200,
            content={
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": config.token_ttl_seconds,
            },
            headers=NO_STORE_HEADERS,
        )

    return router
