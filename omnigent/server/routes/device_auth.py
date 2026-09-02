"""OAuth 2.0 Device Authorization Grant endpoints (RFC 8628).

A generic delegated-login mechanism: any browserless client obtains a
delegated, per-user access token without a user credential ever passing
through the client. The client requests an authorization, relays a
verification link to the user, the user authenticates and consents in
their own browser, and the client polls for a token. The Slack
integration is the first consumer, but nothing here is Slack-specific —
the requesting application names itself with the RFC 8628 ``client_id``
(a public string like ``"slack"``; display + audit only).

Endpoints (all mounted at the app root):

- ``POST /oauth/device/authorize`` — issues ``device_code`` +
  ``user_code`` + verification URIs.
- ``GET  /oauth/device`` — the consent page; requires a browser
  identity (bounces through the provider's login when absent).
- ``POST /oauth/device/approve`` / ``POST /oauth/device/deny`` —
  authenticated browser actions binding the grant to the identity.
- ``POST /oauth/token`` — the client's polling / refresh endpoint;
  returns delegated access + refresh tokens.
- ``POST /oauth/revoke`` — revoke a grant (backs client logout).

Mounted only in ``accounts`` auth mode (and only when
``OMNIGENT_DEVICE_GRANT_ENABLED`` is set). OIDC deployments delegate login
to the IdP via the cli-ticket flow (``/auth/cli-login``) and never use this
grant; header mode has no server-mintable identity.

See ``designs/DEVICE_AUTH.md`` for the full design + threat model.

The security boundary is the secret ``device_code`` the client holds, the
ephemeral verification link, and the authenticated in-browser consent step
— backed by a short device_code TTL, a 30-day absolute grant lifetime,
per-IP rate limiting on authorize, and the consent-page warning against
approving an unexpected login.

Optionally, setting ``OMNIGENT_DEVICE_CLIENT_SECRET`` on the server gates
the CLIENT-facing endpoints (authorize / token / revoke) behind a shared
secret header, so only an authorized client (e.g. the Slack socket server,
which holds the matching secret and a fixed server URL) can drive the flow.
When unset the endpoints stay public. This is safe to ship to the client
now that its server target is a fixed operator config rather than a
user-supplied URL (which is why the secret was previously removed).

Refresh tokens: short access tokens + rotating, revocable refresh tokens.
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import time
from collections.abc import Callable

import jwt
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore, hash_secret
from omnigent.server.routes._origin import require_trusted_origin

_logger = logging.getLogger(__name__)

# Optional shared secret gating the CLIENT-facing device endpoints
# (authorize / token / revoke). When ``OMNIGENT_DEVICE_CLIENT_SECRET`` is
# set on the server, a client (e.g. the Slack socket server) must present
# it in this header or the request is rejected 401 — so only an authorized
# client can drive the device flow. Unset ⇒ the endpoints stay public
# (backward compatible). The BROWSER endpoints (consent GET / approve /
# deny) are NOT gated by this: they run in the user's browser, which never
# holds the secret; their trust comes from the session cookie + Origin.
_CLIENT_SECRET_ENV = "OMNIGENT_DEVICE_CLIENT_SECRET"
_CLIENT_SECRET_HEADER = "X-Omnigent-Client-Secret"

# Scope granted to delegated (device-grant) access tokens. Restricts them
# to the session-facing APIs a delegated client needs; the auth layer
# refuses admin / user-management paths for a token carrying this scope.
DELEGATED_SCOPE = "sessions"

# Reserved ``client_id`` for a first-party login grant (issued by
# ``omnigent login``, not the RFC 8628 device flow). It is a
# security-decision key here, so the device-authorize endpoint REFUSES a
# request that names it — a third-party device client can never obtain a
# grant tagged this way, and a refresh of such a grant is therefore safe
# to treat as first-party. See :func:`issue_login_grant`,
# ``_is_login_grant``, and the reservation guard in ``device_authorize``.
LOGIN_GRANT_CLIENT_ID = "omnigent-cli"


def _is_login_grant(client_id: str | None) -> bool:
    """Return True for a first-party login grant (vs. a device grant).

    Trustworthy because :data:`LOGIN_GRANT_CLIENT_ID` is reserved: the
    device-authorize path rejects it, so only the server-side login flow
    can create a grant carrying it.
    """
    return client_id == LOGIN_GRANT_CLIENT_ID


# RFC 8628 timings.
_DEVICE_CODE_TTL_SECONDS = 600  # 10 min — bounds the unapproved window.
_POLL_INTERVAL_SECONDS = 5  # minimum client poll interval.
# Delegated access tokens are deliberately short-lived; the client
# refreshes silently. A stolen access token expires within the hour.
_ACCESS_TOKEN_TTL_SECONDS = 3600
# Absolute lifetime of an approved grant. Refresh is refused past this, so
# a delegated grant can't be silently refreshed forever — the user must
# re-consent through the flow. Bounds the blast radius of a leaked/phished
# grant to this window even if revocation is never called.
_GRANT_MAX_LIFETIME_SECONDS = 30 * 24 * 3600  # 30 days
# Operator override for the grant lifetime, in whole days. Deployments
# running unattended hosts can extend the re-consent window deliberately;
# the 30-day default stays the safe posture.
_GRANT_MAX_LIFETIME_ENV = "OMNIGENT_GRANT_MAX_LIFETIME_DAYS"


def _grant_max_lifetime_seconds() -> int:
    """Return the grant's absolute lifetime, honoring the env override.

    Invalid or non-positive values fall back to the default — auth
    lifetimes must never fail open to "unbounded" on a typo.
    """
    raw = os.environ.get(_GRANT_MAX_LIFETIME_ENV, "").strip()
    if raw:
        try:
            days = int(raw)
        except ValueError:
            _logger.warning(
                "%s=%r is not an integer — using the %d-day default",
                _GRANT_MAX_LIFETIME_ENV,
                raw,
                _GRANT_MAX_LIFETIME_SECONDS // 86400,
            )
        else:
            if days > 0:
                return days * 86400
            _logger.warning(
                "%s=%r must be positive — using the %d-day default",
                _GRANT_MAX_LIFETIME_ENV,
                raw,
                _GRANT_MAX_LIFETIME_SECONDS // 86400,
            )
    return _GRANT_MAX_LIFETIME_SECONDS


# user_code alphabet excludes easily-confused chars (0/O, 1/I/L).
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_user_code() -> str:
    """Return a short, human-readable ``XXXX-XXXX`` verification code."""
    chars = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _client_id(body: dict[str, object]) -> str | None:
    """Extract the RFC 8628 ``client_id`` from an authorize body.

    A public string naming the requesting application (e.g. Slack passes
    ``"slack"``), the same for every grant that application initiates.
    Display + audit only for device grants — but see
    :data:`LOGIN_GRANT_CLIENT_ID`, which is reserved and refused here.

    Non-string values (``{"client_id": 123}``) read as absent rather than
    raising, so a malformed body is a clean 400 and not a 500.
    """
    raw = body.get("client_id")
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _mint_refresh_token() -> str:
    """Return a fresh high-entropy refresh token (raw; stored hashed)."""
    return secrets.token_urlsafe(32)


def mint_delegated_token(
    user_id: str,
    cookie_secret: bytes,
    ttl_seconds: int,
    provider: str,
    *,
    grant_id: str,
    client_id: str,
    jti: str,
    scope: str | None = DELEGATED_SCOPE,
) -> str:
    """Mint a grant-derived access token.

    Same HS256 shape as
    :func:`omnigent.server.oidc.mint_session_token` (so
    :meth:`UnifiedAuthProvider._check_cookie` validates it unchanged),
    plus grant claims:

    - ``scope`` — present ⇒ the auth layer restricts the token to the
      session APIs and refuses admin endpoints. ``None`` omits the claim,
      giving the token the SAME authority as the session JWT it renews —
      used ONLY for first-party login grants, whose bearer is the
      authenticated user's own CLI/host, not a third-party client.
    - ``grant_id`` — the grant this token was issued from, checked against
      the revocation denylist so revoking the grant immediately kills the
      token. Carried regardless of scope.
    - ``jti`` — unique token id, for audit/log correlation.
    - ``act`` — provenance (RFC 8693 style), ``{"client_id": "<app>"}``,
      naming the application that obtained the grant so every action is
      attributable to it.

    :param user_id: The Omnigent identity the token acts as (``sub``).
    :param cookie_secret: HMAC key for HS256 signing.
    :param ttl_seconds: Token lifetime in seconds (kept short — ≤ 1 h).
    :param provider: Identity provider name (informational claim).
    :param grant_id: The grant id.
    :param client_id: The client id (the requesting application,
        e.g. ``"slack"``); recorded in the ``act`` claim for audit.
    :param jti: Unique token id.
    :param scope: Granted scope, or ``None`` for a full-authority
        (login-grant) token. Defaults to :data:`DELEGATED_SCOPE`.
    :returns: An HS256-signed JWT string.
    """
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "provider": provider,
        "grant_id": grant_id,
        "jti": jti,
        "act": {"client_id": client_id},
    }
    if scope is not None:
        payload["scope"] = scope
    return jwt.encode(payload, cookie_secret, algorithm="HS256")


def _oauth_error(error: str, status_code: int = 400) -> JSONResponse:
    """Return an RFC 6749 / 8628 shaped OAuth error response."""
    return JSONResponse(status_code=status_code, content={"error": error})


def _require_browser_origin(request: Request) -> None:
    """Strict CSRF gate for the browser-only consent POSTs.

    ``require_trusted_origin`` deliberately fail-opens on a *missing*
    ``Origin`` (backward-compat for non-browser first-party clients). The
    approve/deny endpoints, though, are ONLY ever submitted by a real
    browser form, so a missing ``Origin`` is anomalous — reject it here
    (in addition to the shared untrusted-origin check) so the CSRF
    defense does not depend on the session cookie's ``SameSite`` setting.

    :raises HTTPException: 403 when ``Origin`` is absent or untrusted.
    """
    if not request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="missing Origin")
    require_trusted_origin(request)


# ── Abuse controls for the public authorize endpoint ─────────────────
# The authorize endpoint is unauthenticated (public client), so it needs
# its own throttle: without one an attacker could flood it to exhaust the
# grants table. A coarse per-client sliding window is enough — the flow is
# low-volume (one login per user per month or so).
_AUTHORIZE_RATE_MAX = 10  # max authorize calls…
_AUTHORIZE_RATE_WINDOW_SECONDS = 60  # …per client per this window.
# Purge expired/dead grants at most this often (piggybacked on authorize
# so no scheduler is required — keeps the table bounded under load).
_PURGE_MIN_INTERVAL_SECONDS = 300


# Hard cap on distinct keys the limiter tracks at once. Bounds memory even
# under a spray from many source IPs (e.g. a whole IPv6 /64) — without it a
# key hit once and never revisited would live forever. When the cap is hit
# the whole table is swept of aged-out keys; if still full, the limiter
# fails OPEN for a new key (availability over a soft throttle — the real
# anti-abuse control in production is the confidential client secret).
_RATE_LIMITER_MAX_KEYS = 10_000


class _SlidingWindowRateLimiter:
    """Minimal per-key sliding-window limiter (in-memory, single-process).

    Keyed by client IP. Adequate for a single-process socket-mode
    deployment; a multi-replica server would want a shared store, but the
    grant table's own single-use/expiry semantics already bound abuse.

    Memory is bounded by :data:`_RATE_LIMITER_MAX_KEYS`: keys are dropped
    when they age out (on touch) and, when the cap is reached, a full sweep
    reclaims every aged-out key before admitting a new one.
    """

    def __init__(self, max_events: int, window_seconds: int, max_keys: int) -> None:
        self._max = max_events
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: dict[str, list[float]] = {}

    def _sweep(self, cutoff: float) -> None:
        """Drop every key whose hits have all aged out."""
        dead = [k for k, ts in self._hits.items() if not any(t > cutoff for t in ts)]
        for k in dead:
            self._hits.pop(k, None)

    def allow(self, key: str, now: float) -> bool:
        cutoff = now - self._window
        # New key while at capacity: sweep aged-out keys first; if the table
        # is still full of live keys, fail open rather than grow unbounded.
        if key not in self._hits and len(self._hits) >= self._max_keys:
            self._sweep(cutoff)
            if len(self._hits) >= self._max_keys:
                return True
        hits = [t for t in self._hits.get(key, ()) if t > cutoff]
        # Opportunistically bound memory: drop keys that fully aged out.
        if not hits:
            self._hits.pop(key, None)
        if len(hits) >= self._max:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


def _resolve_signing_config(auth_provider: UnifiedAuthProvider) -> tuple[bytes, str]:
    """Return ``(cookie_secret, provider_name)`` for token minting.

    Works for both server-mintable providers: ``accounts`` and ``oidc``.
    Header mode has no server-held signing identity, so grant routes
    cannot be built for it.
    """
    if auth_provider._source == "accounts":
        config = auth_provider._accounts_config
        assert config is not None, "accounts mode must have an accounts config"
        return config.cookie_secret, auth_provider._source
    if auth_provider._source == "oidc":
        oidc_config = auth_provider._oidc_config
        assert oidc_config is not None, "oidc mode must have an oidc config"
        return oidc_config.cookie_secret, auth_provider._source
    raise RuntimeError(
        f"grant routes require accounts or oidc auth (got {auth_provider._source!r})"
    )


def _make_client_secret_gate() -> Callable[[Request], bool]:
    """Build the optional shared-secret check for client-facing endpoints.

    Reads the env once at mount (toggling requires a restart, consistent
    with the other auth env vars). Open when no secret is configured.
    """
    client_secret = os.environ.get(_CLIENT_SECRET_ENV, "").strip() or None
    if client_secret is not None:
        _logger.info("device-auth: client-secret enforcement enabled")

    def _client_secret_ok(request: Request) -> bool:
        if client_secret is None:
            return True
        # Compare on bytes: compare_digest raises TypeError on non-ASCII str
        # operands, and ASGI decodes header bytes as latin-1, so a crafted
        # non-ASCII header would otherwise 500 instead of cleanly failing.
        presented = request.headers.get(_CLIENT_SECRET_HEADER, "")
        return hmac.compare_digest(presented.encode("utf-8"), client_secret.encode("utf-8"))

    return _client_secret_ok


def issue_login_grant(
    device_grant_store: DeviceGrantStore,
    *,
    user_id: str,
    cookie_secret: bytes,
) -> str:
    """Create a redeemed refresh grant for an interactive login.

    Called by the login flows (OIDC cli-ticket fulfillment, accounts
    ``/auth/login``) so the CLI walks away with refresh material and can
    renew its access without a human re-running ``omnigent login``. The
    interactive login *is* the consent step, so the grant is born
    ``redeemed`` and tagged with the reserved
    :data:`LOGIN_GRANT_CLIENT_ID` — which the device-authorize path
    refuses, so this authority class can only originate here.

    :param device_grant_store: Grant persistence.
    :param user_id: The just-authenticated identity.
    :param cookie_secret: HMAC key for hashing the refresh token.
    :returns: The raw refresh token to hand to the client (stored hashed).
    """
    refresh_token = _mint_refresh_token()
    device_grant_store.create_redeemed_grant(
        secrets.token_urlsafe(24),
        user_id=user_id,
        client_id=LOGIN_GRANT_CLIENT_ID,
        refresh_token_hash=hash_secret(refresh_token, cookie_secret),
        created_at=int(time.time()),
    )
    return refresh_token


def create_oauth_token_router(
    auth_provider: UnifiedAuthProvider,
    device_grant_store: DeviceGrantStore,
    *,
    handle_device_code: Callable[[str], Response] | None = None,
    client_secret_ok: Callable[[Request], bool] | None = None,
) -> APIRouter:
    """Build the ``/oauth/token`` + ``/oauth/revoke`` router.

    The refresh/revoke half of the grant machinery, mountable on its own:
    login-issued refresh grants (see :func:`issue_login_grant`) need these
    endpoints in **both** accounts and OIDC modes, independent of the
    RFC 8628 device-code consent flow (which stays accounts-only behind
    ``OMNIGENT_DEVICE_GRANT_ENABLED``).

    :param auth_provider: The active provider — ``accounts`` or ``oidc``.
    :param device_grant_store: Persistence for grants.
    :param handle_device_code: Optional device-code grant handler.
        :func:`create_device_auth_router` passes its polling closure so
        the full flow keeps one token endpoint; standalone mounts leave
        it ``None`` and ``device_code`` exchanges get
        ``unsupported_grant_type``.
    :param client_secret_ok: Optional client-secret gate (callable that validates
        the request). When ``None`` (standalone mounts), builds the gate from
        the ``OMNIGENT_DEVICE_CLIENT_SECRET`` env var. When provided
        (from :func:`create_device_auth_router`), reuses the gate to avoid
        duplication.
    :returns: APIRouter to mount at the app root.
    """
    cookie_secret, provider_name = _resolve_signing_config(auth_provider)
    _client_secret_ok = client_secret_ok or _make_client_secret_gate()
    # Resolve grant max lifetime once at mount time (not on every refresh).
    _grant_max_lifetime = _grant_max_lifetime_seconds()
    router = APIRouter()
    # Throttle for the opportunistic grant purge (bounds the table without a
    # separate scheduler). Mutable one-field dict so the closures can update
    # it. ``0.0`` forces a purge on the first refresh after boot.
    _last_purge = {"at": 0.0}

    def _maybe_purge() -> None:
        """Purge expired/aged grants at most once per interval.

        The device flow purges on ``/oauth/device/authorize``, but the
        standalone token router (OIDC, or accounts without the device flow)
        has no authorize route — so login grants would otherwise accumulate
        one row per login. Piggyback the purge on refresh instead.
        """
        now_wall = time.time()
        if now_wall - _last_purge["at"] < _PURGE_MIN_INTERVAL_SECONDS:
            return
        _last_purge["at"] = now_wall
        try:
            device_grant_store.purge_expired(
                int(now_wall), max_lifetime_seconds=_grant_max_lifetime
            )
        except Exception:  # noqa: BLE001 — housekeeping must never fail a refresh
            _logger.debug("oauth/token: opportunistic grant purge failed", exc_info=True)

    def _issue_access_token(grant_id: str, user_id: str, client_id: str) -> str:
        # A first-party login grant renews with the SAME authority as the
        # session JWT it replaces (scope=None); a third-party device grant
        # stays restricted to the delegated allowlist.
        scope = None if _is_login_grant(client_id) else DELEGATED_SCOPE
        return mint_delegated_token(
            user_id,
            cookie_secret,
            _ACCESS_TOKEN_TTL_SECONDS,
            provider_name,
            grant_id=grant_id,
            client_id=client_id or "",
            jti=secrets.token_urlsafe(16),
            scope=scope,
        )

    @router.post("/oauth/token", dependencies=[])
    async def token(request: Request) -> Response:
        """Exchange a device_code or refresh_token for an access token.

        RFC 8628 / 6749 error shapes: ``authorization_pending``,
        ``slow_down``, ``expired_token``, ``access_denied``,
        ``invalid_grant``, ``unsupported_grant_type``.
        """
        form = await request.form()
        grant_type = str(form.get("grant_type") or "")

        if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
            # The device-code exchange mints from the ephemeral device_code
            # alone, so it stays behind the client-secret gate. Refresh
            # presents the refresh token itself as the credential, so it is
            # not additionally gated (a CLI/host renewing its own login has
            # no way to carry the device client secret).
            if not _client_secret_ok(request):
                return _oauth_error("invalid_client", status_code=401)
            if handle_device_code is None:
                return _oauth_error("unsupported_grant_type")
            return handle_device_code(str(form.get("device_code") or ""))
        if grant_type == "refresh_token":
            return _handle_refresh_grant(str(form.get("refresh_token") or ""))
        return _oauth_error("unsupported_grant_type")

    def _handle_refresh_grant(refresh_token: str) -> Response:
        if not refresh_token:
            return _oauth_error("invalid_request")
        # Opportunistic housekeeping so login-grant rows (one per login)
        # don't accumulate where no device-authorize purge runs.
        _maybe_purge()
        presented_hash = hash_secret(refresh_token, cookie_secret)
        # A refresh token doesn't name its grant, so locate it by digest.
        # Only a live (redeemed, non-revoked) grant holds a matching hash.
        grant = device_grant_store.get_by_refresh_hash(presented_hash)
        if grant is None:
            # Not the current token. If it matches a grant's *previous*
            # token, a stale token was replayed — reuse/theft. Revoke the
            # whole grant so the attacker's freshly-rotated token dies too.
            # (Login grants don't rotate, so they never populate the prev
            # hash and can't reach this revoke.)
            stale = device_grant_store.get_by_prev_refresh_hash(presented_hash)
            if stale is not None:
                device_grant_store.revoke(stale.id)
                _logger.warning(
                    "oauth/token: refresh reuse detected on grant %s — revoked", stale.id
                )
            return _oauth_error("invalid_grant")
        # Refuse to refresh a grant past its absolute lifetime — the user
        # must re-consent. Checked before rotating so an aged grant simply
        # stops working (it is NOT reuse, so it must not revoke/oscillate).
        if grant.approved_at is not None and (
            int(time.time()) - grant.approved_at >= _grant_max_lifetime
        ):
            return _oauth_error("expired_token")
        if grant.user_id is None:
            return _oauth_error("invalid_grant")

        if _is_login_grant(grant.client_id):
            # First-party login grants do NOT rotate. The bearer is an
            # unattended host/CLI, so a lost refresh response (network blip,
            # crash between the server committing and the client persisting)
            # must not brick the grant via reuse detection. The same refresh
            # token stays valid for the grant lifetime; only the short-lived
            # access token is renewed. Revocation + the absolute lifetime cap
            # bound the exposure.
            access_token = _issue_access_token(grant.id, grant.user_id, grant.client_id or "")
            return JSONResponse(
                status_code=200,
                content={
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "token_type": "Bearer",
                    "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
                },
            )

        new_refresh = _mint_refresh_token()
        rotated = device_grant_store.rotate_refresh_token(
            grant.id,
            expected_hash=presented_hash,
            new_hash=hash_secret(new_refresh, cookie_secret),
            now_epoch_seconds=int(time.time()),
            max_lifetime_seconds=_grant_max_lifetime,
        )
        if rotated is None:
            # Lost a concurrent rotation race, or the grant aged out between
            # the check above and here — reject without revoking (this is not
            # a reuse signal, so the grant must not be killed/oscillate).
            return _oauth_error("invalid_grant")
        if rotated.user_id is None:
            return _oauth_error("invalid_grant")
        access_token = _issue_access_token(
            rotated.id,
            rotated.user_id,
            rotated.client_id or "",
        )
        return JSONResponse(
            status_code=200,
            content={
                "access_token": access_token,
                "refresh_token": new_refresh,
                "token_type": "Bearer",
                "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
            },
        )

    # ── Revocation ────────────────────────────────────────────────

    @router.post("/oauth/revoke", dependencies=[])
    async def revoke(request: Request) -> Response:
        """Revoke a grant by refresh token or by the caller's access token.

        Backs ``/omnigent logout``. Accepts a ``refresh_token`` form
        field; falls back to the ``grant_id`` on the caller's own
        delegated access token so a client with only its access token
        can still log out. Not behind the device client-secret gate: the
        presented refresh/access token IS the credential, and a CLI
        logging out its own login grant cannot carry that secret.
        """
        form = await request.form()
        refresh_token = str(form.get("refresh_token") or "")
        grant = None
        if refresh_token:
            grant = device_grant_store.get_by_refresh_hash(
                hash_secret(refresh_token, cookie_secret)
            )
        if grant is None:
            grant_id = _grant_id_from_bearer(request)
            if grant_id is not None:
                grant = device_grant_store.get_by_id(grant_id)
        if grant is None:
            # Idempotent: nothing to revoke is still "revoked" from the
            # caller's perspective. Don't leak which tokens exist.
            return JSONResponse(status_code=200, content={"revoked": True})
        device_grant_store.revoke(grant.id)
        _logger.info("oauth/revoke: revoked grant %s", grant.id)
        return JSONResponse(status_code=200, content={"revoked": True})

    def _grant_id_from_bearer(request: Request) -> str | None:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        try:
            payload = jwt.decode(auth_header[7:], cookie_secret, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return None
        grant_id = payload.get("grant_id")
        return grant_id if isinstance(grant_id, str) else None

    return router


def create_device_auth_router(
    auth_provider: UnifiedAuthProvider,
    device_grant_store: DeviceGrantStore,
) -> APIRouter:
    """Build the ``/oauth/*`` device-grant router.

    :param auth_provider: The active provider. Must be ``accounts`` mode;
        its cookie config supplies the HMAC signing key and public base URL.
    :param device_grant_store: Persistence for device grants.
    :returns: APIRouter to mount at the app root.
    """
    if auth_provider._source != "accounts":
        raise RuntimeError(
            f"create_device_auth_router requires accounts auth (got {auth_provider._source!r})"
        )
    cookie_config = auth_provider._accounts_config
    assert cookie_config is not None, "accounts mode must have an accounts config"
    cookie_secret = cookie_config.cookie_secret
    base_url = cookie_config.base_url
    provider_name = auth_provider._source

    _client_secret_ok = _make_client_secret_gate()
    # Resolve grant max lifetime once at mount (not on every purge).
    _grant_max_lifetime = _grant_max_lifetime_seconds()

    router = APIRouter()
    _rate_limiter = _SlidingWindowRateLimiter(
        _AUTHORIZE_RATE_MAX, _AUTHORIZE_RATE_WINDOW_SECONDS, _RATE_LIMITER_MAX_KEYS
    )
    # Last time we purged expired grants; gates the opportunistic purge on
    # authorize so the table stays bounded without a separate scheduler.
    _last_purge = {"at": 0.0}

    def _issue_access_token(grant_id: str, user_id: str, client_id: str) -> str:
        return mint_delegated_token(
            user_id,
            cookie_secret,
            _ACCESS_TOKEN_TTL_SECONDS,
            provider_name,
            grant_id=grant_id,
            client_id=client_id or "",
            jti=secrets.token_urlsafe(16),
        )

    # ── Device authorization (public) ─────────────────────────────

    @router.post("/oauth/device/authorize", dependencies=[])
    async def device_authorize(request: Request) -> Response:
        """Start a device flow (public — anyone may initiate).

        Nothing is granted here: the grant is ``pending`` until an
        authenticated user approves it in a browser. The ``client_id`` is
        recorded for the consent screen and the issued token's audit
        ``act`` claim.

        Rate-limited per client IP, and opportunistically purges expired
        grants so the table stays bounded.
        """
        if not _client_secret_ok(request):
            return _oauth_error("invalid_client", status_code=401)
        now_wall = time.time()
        client_ip = request.client.host if request.client else "unknown"
        if not _rate_limiter.allow(client_ip, now_wall):
            return _oauth_error("slow_down", status_code=429)

        # Opportunistic housekeeping: reclaim expired/dead grants at most
        # once per interval (no scheduler needed).
        if now_wall - _last_purge["at"] >= _PURGE_MIN_INTERVAL_SECONDS:
            _last_purge["at"] = now_wall
            try:
                device_grant_store.purge_expired(
                    int(now_wall), max_lifetime_seconds=_grant_max_lifetime
                )
            except Exception:  # housekeeping must never fail a request
                _logger.exception("device grant purge failed")

        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        client_id = _client_id(body)
        # LOGIN_GRANT_CLIENT_ID marks a first-party login grant, whose
        # refreshed tokens carry full session authority. It is reserved:
        # a device client must never be able to self-declare into that
        # class by naming it here.
        if _is_login_grant(client_id):
            _logger.warning(
                "device/authorize: refused reserved client_id %r", LOGIN_GRANT_CLIENT_ID
            )
            return _oauth_error("invalid_request")

        device_code = secrets.token_urlsafe(32)
        grant_id = secrets.token_urlsafe(16)
        user_code = _generate_user_code()
        now = int(time.time())
        device_grant_store.create_grant(
            grant_id,
            device_code_hash=hash_secret(device_code, cookie_secret),
            user_code=user_code,
            client_id=client_id,
            created_at=now,
            expires_at=now + _DEVICE_CODE_TTL_SECONDS,
        )
        verification_uri = f"{base_url}/oauth/device"
        verification_uri_complete = f"{verification_uri}?user_code={user_code}"
        _logger.info("device/authorize: issued grant for client=%s", client_id)
        return JSONResponse(
            status_code=200,
            content={
                "device_code": device_code,
                "user_code": user_code,
                "verification_uri": verification_uri,
                "verification_uri_complete": verification_uri_complete,
                "expires_in": _DEVICE_CODE_TTL_SECONDS,
                "interval": _POLL_INTERVAL_SECONDS,
            },
        )

    # ── Browser consent page ──────────────────────────────────────

    def _bounce_to_login(user_code: str, *, reauth: bool) -> RedirectResponse:
        """302 to the login page, returning to this consent URL afterward.

        ``reauth`` adds ``&reauth=1`` so the login page forces a fresh
        password submit instead of auto-bouncing an already-signed-in user
        (which would loop back here with the same stale session).
        """
        login_url = auth_provider.login_url or "/login"
        return_to = f"/oauth/device?user_code={user_code}" if user_code else "/oauth/device"
        query = f"return_to={html.escape(return_to, quote=True)}"
        if reauth:
            query += "&reauth=1"
        return RedirectResponse(url=f"{login_url}?{query}", status_code=302)

    def _session_iat(request: Request) -> int | None:
        """Return the ``iat`` (issue time) of the caller's session JWT.

        Read from the session cookie (accounts mode mints a fresh ``iat``
        on every ``/auth/login``, so this is effectively the last-login
        time). ``None`` when absent/invalid. Used to enforce that consent
        follows a login started FOR this device flow.
        """
        token = request.cookies.get(cookie_config.session_cookie_name)
        if not token:
            return None
        try:
            payload = jwt.decode(token, cookie_secret, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return None
        iat = payload.get("iat")
        return iat if isinstance(iat, int) else None

    @router.get("/oauth/device")
    async def device_consent_page(request: Request) -> Response:
        """Render the consent page for a ``user_code``.

        Requires a browser identity; if the user is not signed in,
        bounce through the provider's login and return here (the
        ``return_to`` is sanitized by the login route). Shows the exact
        Omnigent identity being delegated and the requesting ``client_id``
        so any mismatch is visible before approval.

        **Re-authentication:** consent requires a login performed AFTER this
        device flow began (session ``iat`` ≥ the grant's ``created_at``). A
        pre-existing session — however recent — is bounced back through the
        login page with ``reauth=1``, so approving a device grant always
        costs a deliberate, fresh password entry. This closes the
        reflex-approve phishing case: a victim already signed in can't bind
        an attacker's grant with one click; they must re-enter their password
        against a screen naming the exact identity and client.
        """
        user_id = auth_provider.get_user_id(request)
        user_code = (request.query_params.get("user_code") or "").strip()
        if user_id is None:
            return _bounce_to_login(user_code, reauth=False)

        if not user_code:
            return HTMLResponse(_consent_html(prompt_for_code=True), status_code=200)

        grant = device_grant_store.get_by_user_code(user_code)
        now = int(time.time())
        if grant is None or grant.status != "pending" or grant.expires_at <= now:
            return HTMLResponse(
                _consent_html(error="This link is invalid or has expired."),
                status_code=200,
            )

        # Force a fresh login when the current session predates this grant:
        # only a login started for THIS flow (iat ≥ the grant's created_at)
        # may approve. Bounce with reauth=1 so the login page re-prompts
        # rather than auto-returning the stale session (which would loop).
        session_iat = _session_iat(request)
        if session_iat is None or session_iat < grant.created_at:
            return _bounce_to_login(user_code, reauth=True)

        return HTMLResponse(
            _consent_html(
                user_code=user_code,
                user_id=user_id,
                client_id=grant.client_id,
            ),
            status_code=200,
        )

    @router.post("/oauth/device/approve", dependencies=[])
    async def device_approve(request: Request) -> Response:
        """Bind a pending grant to the authenticated identity.

        Guarded by a strict CSRF check (trusted, present ``Origin``) and
        requires the browser identity; binds the approving Omnigent
        identity to the grant.
        """
        _require_browser_origin(request)
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return _oauth_error("unauthorized", status_code=401)

        form = await request.form()
        user_code = (str(form.get("user_code") or "")).strip()
        grant = device_grant_store.get_by_user_code(user_code) if user_code else None
        now = int(time.time())
        if grant is None or grant.status != "pending" or grant.expires_at <= now:
            return HTMLResponse(
                _consent_html(error="This link is invalid or has expired."),
                status_code=200,
            )

        # Re-auth gate, enforced here too (not just on the consent GET): a
        # stale session must not approve by POSTing directly. Only a login
        # started for THIS flow (session iat ≥ the grant's created_at) passes.
        session_iat = _session_iat(request)
        if session_iat is None or session_iat < grant.created_at:
            return HTMLResponse(
                _consent_html(
                    error="Your session is too old to approve this login. "
                    "Start setup again and sign in when prompted."
                ),
                status_code=200,
            )

        approved = device_grant_store.approve(
            grant.id,
            user_id=user_id,
            now_epoch_seconds=now,
        )
        if approved is None:
            return HTMLResponse(
                _consent_html(error="This request could not be approved."),
                status_code=200,
            )
        _logger.info(
            "device/approve: %s approved grant for client=%s",
            user_id,
            grant.client_id,
        )
        return HTMLResponse(
            _consent_html(approved_as=user_id),
            status_code=200,
        )

    @router.post("/oauth/device/deny", dependencies=[])
    async def device_deny(request: Request) -> Response:
        """Deny a pending grant."""
        _require_browser_origin(request)
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return _oauth_error("unauthorized", status_code=401)
        form = await request.form()
        user_code = (str(form.get("user_code") or "")).strip()
        grant = device_grant_store.get_by_user_code(user_code) if user_code else None
        if grant is not None:
            device_grant_store.deny(grant.id)
        return HTMLResponse(_consent_html(denied=True), status_code=200)

    # ── Token + revocation endpoints ──────────────────────────────
    # Shared with the standalone login-grant mount: the device flow's
    # only addition is the device_code grant handler, injected here so
    # /oauth/token stays a single registration either way.

    def _handle_device_code_grant(device_code: str) -> Response:
        if not device_code:
            return _oauth_error("invalid_request")
        now = int(time.time())
        outcome, grant = device_grant_store.poll_for_token(
            hash_secret(device_code, cookie_secret),
            now_epoch_seconds=now,
            min_interval_seconds=_POLL_INTERVAL_SECONDS,
        )
        if outcome == "not_found":
            return _oauth_error("invalid_grant")
        if outcome == "slow_down":
            return _oauth_error("slow_down")
        if outcome == "pending":
            return _oauth_error("authorization_pending")
        if outcome == "denied" or outcome == "revoked":
            return _oauth_error("access_denied")
        if outcome == "expired":
            return _oauth_error("expired_token")
        if outcome == "redeemed":
            # device_code is single-use; a second exchange is rejected.
            return _oauth_error("invalid_grant")
        # outcome == "approved" → mint tokens, atomically single-use.
        assert grant is not None
        refresh_token = _mint_refresh_token()
        redeemed = device_grant_store.redeem_approved(
            grant.id,
            refresh_token_hash=hash_secret(refresh_token, cookie_secret),
            now_epoch_seconds=now,
        )
        if redeemed is None or redeemed.user_id is None:
            # Lost the race (concurrent poll already redeemed) or expired.
            return _oauth_error("invalid_grant")
        access_token = _issue_access_token(
            redeemed.id,
            redeemed.user_id,
            redeemed.client_id or "",
        )
        _logger.info("oauth/token: issued delegated token for grant %s", redeemed.id)
        return JSONResponse(
            status_code=200,
            content={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
            },
        )

    router.include_router(
        create_oauth_token_router(
            auth_provider,
            device_grant_store,
            handle_device_code=_handle_device_code_grant,
            client_secret_ok=_client_secret_ok,
        )
    )

    return router


def _consent_html(
    *,
    user_code: str = "",
    user_id: str = "",
    client_id: str | None = None,
    prompt_for_code: bool = False,
    error: str = "",
    approved_as: str = "",
    denied: bool = False,
) -> str:
    """Render the minimal, dependency-free consent page.

    Client-agnostic: the initiating client is shown via its ``client_id``.
    All interpolated values are HTML-escaped. The page is intentionally
    self-contained (no JS framework) so it works regardless of the
    server's front-end build.
    """

    def esc(value: object) -> str:
        return html.escape(str(value or ""))

    # Requesting client's identifier, defaulting to a neutral label when it
    # didn't identify itself.
    app_name = esc(client_id) if client_id else "An application"
    if error:
        body = f'<p class="err">{esc(error)}</p>'
    elif approved_as:
        body = (
            f"<h1>Connected</h1><p>{app_name} is now authorized to act as "
            f"<b>{esc(approved_as)}</b>. You can close this tab.</p>"
        )
    elif denied:
        body = "<h1>Denied</h1><p>No access was granted. You can close this tab.</p>"
    elif prompt_for_code:
        body = (
            "<h1>Link your account</h1>"
            '<form method="get" action="/oauth/device">'
            "<label>Enter the code shown by the application:"
            '<input name="user_code" autofocus placeholder="XXXX-XXXX"></label>'
            '<button type="submit">Continue</button></form>'
        )
    else:
        body = (
            "<h1>Authorize access</h1>"
            f"<p>{app_name} is requesting permission to act as "
            f"<b>{esc(user_id)}</b> on this Omnigent server.</p>"
            f'<p class="muted">Code: {esc(user_code)}</p>'
            '<p class="warn">⚠️ Only approve if <b>you</b> just started this '
            "login and this code matches the one the application showed you. If "
            "you didn't start it, click Deny — approving lets the application "
            "act as you.</p>"
            '<form method="post" action="/oauth/device/approve" class="row">'
            f'<input type="hidden" name="user_code" value="{esc(user_code)}">'
            '<button type="submit" class="primary">Approve</button></form>'
            '<form method="post" action="/oauth/device/deny" class="row">'
            f'<input type="hidden" name="user_code" value="{esc(user_code)}">'
            '<button type="submit">Deny</button></form>'
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Authorize access — Omnigent</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        "padding:0 1rem;line-height:1.5}h1{font-size:1.4rem}.muted{color:#666;"
        "font-size:.9rem}.warn{color:#8a5a00;background:#fff7e6;padding:.6rem .8rem;"
        "border-radius:.375rem;font-size:.9rem}.err{color:#b00}"
        "button{font-size:1rem;padding:.5rem 1rem;"
        "margin:.25rem 0;cursor:pointer}.primary{background:#2563eb;color:#fff;"
        "border:none;border-radius:.375rem}.row{display:inline-block;margin-right:.5rem}"
        "input{font-size:1rem;padding:.4rem;margin:.5rem}</style></head>"
        f"<body>{body}</body></html>"
    )
