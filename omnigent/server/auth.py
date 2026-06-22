"""User identity extraction from incoming requests.

Provides a pluggable :class:`AuthProvider` ABC and a
:class:`UnifiedAuthProvider` that supports three identity sources,
selected via the ``OMNIGENT_AUTH_PROVIDER`` env var:

- ``"header"`` (default): reads the ``X-Forwarded-Email`` header
  from a trusted upstream proxy (override the header name with
  ``OMNIGENT_AUTH_HEADER``, e.g.
  ``Cf-Access-Authenticated-User-Email`` for Cloudflare Access).
  Requests without the header are rejected (401) unless the server
  was explicitly started as a single-user local runtime
  (``OMNIGENT_LOCAL_SINGLE_USER=1``), in which case they fall back
  to the reserved ``"local"`` user.
- ``"oidc"``: reads the ``__Host-ap_session`` signed cookie minted
  after a full OIDC authorization-code+PKCE login flow.
- ``"accounts"``: same signed cookie machinery as OIDC, but minted
  by the built-in username+password ``/auth/login`` endpoint. The
  ``accounts`` provider is the OSS-CUJ-v2 default — first-user-is-admin
  with invite-only signup; see ``designs/oss-cuj/04-implementation-plan.md``.

Cookie validation is identical across OIDC and accounts modes —
both share :class:`AccountsConfig`/:class:`OIDCConfig`-shaped cookie
parameters. The provider is instantiated once at server startup
and closed over by route factories — no per-request import cost.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod

from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)

# Opt-in multi-user switch. ``OMNIGENT_AUTH_ENABLED`` is the current
# name; ``OMNIGENT_ACCOUNTS_ENABLED`` is the pre-rename name, still
# honored as a deprecated alias (see :func:`_auth_enabled`).
_AUTH_ENABLED_ENV = "OMNIGENT_AUTH_ENABLED"
_AUTH_ENABLED_ENV_DEPRECATED = "OMNIGENT_ACCOUNTS_ENABLED"

RESERVED_USER_LOCAL = "local"
RESERVED_USER_PUBLIC = "__public__"
_RESERVED_USERS = frozenset({RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC})
_TRUTHY_STRINGS = ("1", "true", "yes")

# Explicit single-user marker. Set by the managed local-server spawn
# paths (`omnigent run` in chat.py, the daemon's
# host/local_server.py) and by the canonical bare loopback
# `omnigent server` (cli.py) — never by deployed multi-user servers.
# Gates the header-mode "local" fallback (see
# :meth:`UnifiedAuthProvider._check_header`) and host_id re-owning in
# routes/host_tunnel.py.
_LOCAL_SINGLE_USER_ENV = "OMNIGENT_LOCAL_SINGLE_USER"

# Name of the trusted identity header read in header-auth mode.
# Overridable so deploys behind a proxy that uses a different header
# name (e.g. Cloudflare Access' ``Cf-Access-Authenticated-User-Email``)
# work without an extra proxy transform. Defaults to the oauth2-proxy /
# Databricks Apps convention. See :func:`resolve_auth_header`.
_AUTH_HEADER_ENV = "OMNIGENT_AUTH_HEADER"
_DEFAULT_AUTH_HEADER = "X-Forwarded-Email"

LEVEL_READ = 1
LEVEL_EDIT = 2
LEVEL_MANAGE = 3
LEVEL_OWNER = 4


def env_var_is_truthy(name: str, *, default: bool = False) -> bool:
    """Parse a boolean-style environment variable.

    Truthy values match the existing harness env-var convention:
    ``"1"``, ``"true"``, and ``"yes"`` are true
    case-insensitively. Unset or empty values return ``default``;
    every other value is false.

    :param name: Environment variable name.
    :param default: Value to return when the variable is unset or
        empty.
    :returns: Parsed boolean value.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY_STRINGS


def local_single_user_enabled() -> bool:
    """Whether this server is an explicit single-user local runtime.

    Reads ``OMNIGENT_LOCAL_SINGLE_USER``, the marker the managed
    local spawn paths set when starting THE user's own loopback
    server. Deployed multi-user servers never set it, so everything
    it gates (header-mode ``"local"`` fallback, host_id re-owning)
    stays fail-closed there.

    :returns: ``True`` when the single-user marker is set and truthy.
    """
    return env_var_is_truthy(_LOCAL_SINGLE_USER_ENV)


def resolve_auth_header() -> str:
    """Resolve the trusted identity header name for header-auth mode.

    Reads ``OMNIGENT_AUTH_HEADER`` and falls back to
    :data:`_DEFAULT_AUTH_HEADER` (``X-Forwarded-Email``) when unset or
    empty. Header names are case-insensitive per RFC 7230, so the value
    is used as-is — Starlette's ``request.headers`` lookup is itself
    case-insensitive.

    The override exists so a deploy behind a proxy that authenticates
    with a differently-named header can point the server at it directly,
    e.g. ``OMNIGENT_AUTH_HEADER=Cf-Access-Authenticated-User-Email`` for
    Cloudflare Access, instead of standing up an extra hop to rename the
    header to ``X-Forwarded-Email``.

    :returns: The header name to read identity from in header mode.
    """
    raw = os.environ.get(_AUTH_HEADER_ENV, "").strip()
    return raw or _DEFAULT_AUTH_HEADER


_auth_enabled_deprecation_warned = False


def _auth_enabled() -> bool:
    """Whether multi-user auth is opted in via the enable switch.

    Reads ``OMNIGENT_AUTH_ENABLED``. The pre-rename name
    ``OMNIGENT_ACCOUNTS_ENABLED`` is still honored as a deprecated
    alias: when it is set and the current name is not, its value is
    used and a one-time deprecation warning is logged. The current name
    always wins when both are set, so a deploy migrating to the new name
    can leave the old one in place without surprise.

    Both names share the same truthiness rules (see
    :func:`env_var_is_truthy`) and the same explicit-falsy kill-switch
    semantics — ``OMNIGENT_AUTH_ENABLED=0`` disables auth even though
    the var is "set", which is how the Docker entrypoint lets an
    operator opt back out of the default-on accounts mode.

    :returns: ``True`` when multi-user auth should be enabled.
    """
    global _auth_enabled_deprecation_warned
    if os.environ.get(_AUTH_ENABLED_ENV, "").strip():
        return env_var_is_truthy(_AUTH_ENABLED_ENV, default=False)
    if os.environ.get(_AUTH_ENABLED_ENV_DEPRECATED, "").strip():
        if not _auth_enabled_deprecation_warned:
            logger.warning(
                "%s is deprecated; rename it to %s. The old name still "
                "works for now but will be removed in a future release.",
                _AUTH_ENABLED_ENV_DEPRECATED,
                _AUTH_ENABLED_ENV,
            )
            _auth_enabled_deprecation_warned = True
        return env_var_is_truthy(_AUTH_ENABLED_ENV_DEPRECATED, default=False)
    return False


def resolve_auth_source() -> str:
    """
    Resolve the server's auth provider source from the environment.

    Single source of truth for the auth-mode decision so every spawn
    path (``create_auth_provider`` here, the daemon-owned local server in
    ``host/local_server.py``, and the per-command server in ``chat.py``)
    agrees on which mode a server boots in. The rules mirror
    :func:`create_auth_provider`:

    - An explicit ``OMNIGENT_AUTH_PROVIDER`` (case-insensitive) always
      wins, e.g. ``"header"`` / ``"oidc"`` / ``"accounts"``. This is the
      low-level escape hatch.
    - Otherwise ``header`` is the default, unless the opt-in switch
      ``OMNIGENT_AUTH_ENABLED`` is truthy (see :func:`_auth_enabled`,
      which also honors the deprecated ``OMNIGENT_ACCOUNTS_ENABLED``
      alias). When enabled, the mode depends on whether OIDC config was
      supplied:

      - ``OMNIGENT_OIDC_ISSUER`` is set → ``"oidc"`` (the operator
        brought their own IdP). The issuer is the canonical, always-
        required OIDC identifier; :func:`OIDCConfig.from_env` then fails
        loud if the rest of the OIDC config is missing.
      - otherwise → ``"accounts"`` (the built-in username+password
        login flow).

    :returns: The resolved source string, e.g. ``"accounts"``,
        ``"header"``, or ``"oidc"`` (or any explicit lower-cased value of
        ``OMNIGENT_AUTH_PROVIDER``). The caller is responsible for
        rejecting unknown values.
    """
    raw_source = os.environ.get("OMNIGENT_AUTH_PROVIDER")
    if raw_source and raw_source.strip():
        return raw_source.strip().lower()
    # Opt-in multi-user — see create_auth_provider's docstring.
    if _auth_enabled():
        # An operator-supplied OIDC issuer selects the native
        # authorization-code flow; otherwise the built-in accounts flow.
        if os.environ.get("OMNIGENT_OIDC_ISSUER", "").strip():
            return "oidc"
        return "accounts"
    return "header"


class AuthProvider(ABC):
    """Extract a user ID from an incoming request.

    Implementations must return a user ID string or ``None``.
    When ``None`` is returned, the route helpers respond with 401.
    """

    @abstractmethod
    def get_user_id(self, request: HTTPConnection) -> str | None:
        """Return the authenticated user ID, or ``None``."""
        ...


class UnifiedAuthProvider(AuthProvider):
    """Unified authentication provider that supports header-based,
    OIDC, and accounts cookie-based identity extraction.

    Exactly one source is active per deployment, selected by
    ``OMNIGENT_AUTH_PROVIDER``. OIDC and accounts modes share
    the same cookie machinery — the difference is only in how the
    cookie was minted (OIDC IdP callback vs ``/auth/login``).

    :param source: The active identity source: ``"header"``,
        ``"oidc"``, or ``"accounts"``.
    :param oidc_config: OIDC configuration. Required when
        ``source`` is ``"oidc"``, ``None`` otherwise.
    :param accounts_config: Accounts configuration. Required when
        ``source`` is ``"accounts"``, ``None`` otherwise.
    :param local_single_user: When ``True``, header mode falls back
        to the reserved ``"local"`` identity for requests without
        the identity header — the explicit single-user posture of
        the user's own loopback server. When ``False``, such
        requests are rejected (``None`` → 401, fail closed).
        ``None`` (the default) resolves from
        ``OMNIGENT_LOCAL_SINGLE_USER`` at construction (see
        :func:`local_single_user_enabled`). Only consulted in
        header mode. Tests pass an explicit bool.
    :param header_name: The trusted identity header read in header
        mode. ``None`` (the default) resolves from
        ``OMNIGENT_AUTH_HEADER`` at construction, falling back to
        ``X-Forwarded-Email`` (see :func:`resolve_auth_header`).
        Only consulted in header mode. Tests pass an explicit name.
    """

    def __init__(
        self,
        source: str,
        oidc_config: OIDCConfig | None = None,
        accounts_config: AccountsConfig | None = None,
        local_single_user: bool | None = None,
        header_name: str | None = None,
    ) -> None:
        self._source = source
        self._oidc_config = oidc_config
        self._accounts_config = accounts_config
        self._local_single_user = (
            local_single_user if local_single_user is not None else local_single_user_enabled()
        )
        self._header_name = header_name if header_name is not None else resolve_auth_header()
        self._cookie_cache: dict[str, tuple[str, float]] = {}

    @property
    def login_url(self) -> str | None:
        """Where the frontend should redirect on 401.

        - ``"oidc"`` → ``"/auth/login"`` (server-side GET that
          builds the PKCE state cookie and redirects to the IdP's
          authorize endpoint).
        - ``"accounts"`` → ``"/login"`` (SPA route — the React
          ``LoginPage`` renders a username + password form and
          POSTs to ``/auth/login``). Distinct from OIDC because
          accounts mode has no IdP handoff; the form lives in the
          browser.
        - ``"header"`` → ``None`` (no login page; missing identity
          is the proxy's responsibility).
        """
        if self._source == "oidc":
            return "/auth/login"
        if self._source == "accounts":
            return "/login"
        return None

    def get_user_id(self, request: HTTPConnection) -> str | None:
        """Extract user identity from the active source.

        - ``"header"``: Read the configured identity header
          (default ``X-Forwarded-Email``; see
          :func:`resolve_auth_header`).
        - ``"oidc"`` / ``"accounts"``: Read ``__Host-ap_session``
          cookie, validate HS256 signature and expiry, return
          ``sub`` claim.

        :param request: The incoming HTTP request or WebSocket
            handshake (both are ``HTTPConnection``).
        :returns: Authenticated user ID, or ``None`` (→ 401).
        """
        if self._source in ("oidc", "accounts"):
            return self._check_cookie(request)
        return self._check_header(request)

    def _check_cookie(self, request: HTTPConnection) -> str | None:
        """Validate the session cookie or Bearer token and return the
        user ID.

        Checks the session cookie first (browser clients), then
        falls back to ``Authorization: Bearer <jwt>`` (CLI clients
        authenticated via ``omnigent login``). Both carry the same
        HS256-signed JWT.

        Uses a TTL credential cache keyed by HMAC-SHA256 digest of
        the raw token to avoid repeated JWT decoding on every
        request.

        :param request: The incoming HTTP request or WebSocket.
        :returns: User ID from the JWT's ``sub`` claim, or
            ``None`` if no valid token is found.
        """
        import jwt

        from omnigent.server.oidc import hmac_digest

        # Both OIDC and accounts modes use the same cookie machinery
        # — read the active config wherever it lives. The two configs
        # share `cookie_secret` and `session_cookie_name` properties
        # by construction (see AccountsConfig docstring).
        cookie_config = self._oidc_config if self._source == "oidc" else self._accounts_config
        cookie_name = cookie_config.session_cookie_name
        token = request.cookies.get(cookie_name)
        if not token:
            # Fall back to Bearer token for CLI clients.
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
        if not token:
            return None

        cache_key = hmac_digest(token, cookie_config.cookie_secret)
        cached = self._cookie_cache.get(cache_key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]

        try:
            payload = jwt.decode(
                token,
                cookie_config.cookie_secret,
                algorithms=["HS256"],
            )
        except jwt.InvalidTokenError:
            return None

        user_id = payload.get("sub")
        if not user_id or user_id in _RESERVED_USERS:
            return None

        # Cache for remaining lifetime of the token.
        remaining = payload.get("exp", 0) - time.time()
        if remaining > 0:
            self._cookie_cache[cache_key] = (
                user_id,
                time.monotonic() + remaining,
            )

        return user_id

    def _check_header(self, request: HTTPConnection) -> str | None:
        """Read the trusted identity header and return the user ID.

        The header name is :attr:`_header_name` (``X-Forwarded-Email``
        by default, overridable via ``OMNIGENT_AUTH_HEADER`` — e.g.
        ``Cf-Access-Authenticated-User-Email`` for Cloudflare Access).

        When the header is present, its value is used as the identity
        (reserved names like ``"local"`` are rejected). When absent,
        the request is rejected (``None`` → 401): a missing or
        dropped proxy header must fail closed, never resolve to a
        shared default identity that every unauthenticated request
        would then share.

        The one exception is the explicit single-user local runtime
        (``local_single_user=True``, from
        ``OMNIGENT_LOCAL_SINGLE_USER=1``): there the absent header
        falls back to :data:`RESERVED_USER_LOCAL`, because the
        server's only user IS the local user and no proxy exists to
        inject identity.

        :param request: The incoming HTTP request or WebSocket.
        :returns: User ID from the header; ``"local"`` when the
            header is absent on a single-user local runtime; else
            ``None`` (→ 401).
        """
        email = request.headers.get(self._header_name)
        if email:
            if email in _RESERVED_USERS:
                return None
            return email
        if self._local_single_user:
            return RESERVED_USER_LOCAL
        return None


def create_auth_provider() -> AuthProvider:
    """Factory: read ``OMNIGENT_AUTH_PROVIDER`` and return a
    :class:`UnifiedAuthProvider` configured for the selected source.

    Defaults to ``"header"`` when the env var is unset — a bare
    ``omnigent server`` is single-user, no-login out of the box.
    Header mode rejects requests without the configured identity
    header (default ``X-Forwarded-Email``, overridable via
    ``OMNIGENT_AUTH_HEADER``) — 401, fail closed; see
    :meth:`UnifiedAuthProvider._check_header` — unless the server
    is an explicit single-user local runtime
    (``OMNIGENT_LOCAL_SINGLE_USER=1``, set by the managed local
    spawn paths and the canonical bare loopback ``omnigent
    server``), where the absent header falls back to the reserved
    ``"local"`` user — the convenient posture for local development
    without minting cookies / typing passwords.

    Opt-in multi-user (accounts / OIDC)
    -----------------------------------
    Set ``OMNIGENT_AUTH_ENABLED=1`` (or any truthy value) to turn on
    multi-user auth. With no OIDC config present this selects
    ``accounts`` mode — the built-in login flow with
    first-user-is-admin setup. Set the ``OMNIGENT_OIDC_*`` env vars
    (at minimum ``OMNIGENT_OIDC_ISSUER``) alongside it and the same
    switch instead selects ``oidc`` — the native authorization-code
    flow against your own IdP. Containerized / remote deploys (Docker,
    HF Spaces, Render, Railway) flip this on in their entrypoints so a
    deployed instance is authenticated by default; a bare local server
    leaves it off. An explicit ``OMNIGENT_AUTH_PROVIDER`` always wins
    over this switch — it only governs the env-unset default. Deploys
    behind an SSO proxy that injects ``X-Forwarded-Email`` set
    ``OMNIGENT_AUTH_PROVIDER=header`` (Databricks Apps, oauth2-proxy);
    proxies that authenticate with a different header name also set
    ``OMNIGENT_AUTH_HEADER`` (e.g.
    ``Cf-Access-Authenticated-User-Email`` for Cloudflare Access — see
    :func:`resolve_auth_header`).

    (``OMNIGENT_AUTH_ENABLED`` is the renamed opt-in gate,
    commit ``b23e886e``, formerly ``OMNIGENT_ACCOUNTS_ENABLED``:
    header is the shipped default, so the var is an enable switch, not
    a kill switch. The old name is still honored as a deprecated
    alias — see :func:`_auth_enabled`.)

    Validates the source's required env vars at startup (fail
    loud) — OIDC fetches the discovery document, accounts decodes
    the cookie secret.

    :returns: Configured auth provider.
    :raises RuntimeError: On unknown source or invalid config.
    """
    source = resolve_auth_source()

    if source not in ("header", "oidc", "accounts"):
        raise RuntimeError(
            f"Unknown OMNIGENT_AUTH_PROVIDER={source!r}. Valid: 'header', 'oidc', 'accounts'"
        )

    oidc_config: OIDCConfig | None = None
    accounts_config: AccountsConfig | None = None
    if source == "oidc":
        from omnigent.server.oidc import OIDCConfig

        oidc_config = OIDCConfig.from_env()
    elif source == "accounts":
        # Reaching here means accounts mode was deliberately selected
        # — either OMNIGENT_AUTH_PROVIDER=accounts or the
        # OMNIGENT_AUTH_ENABLED=1 opt-in without OIDC config
        # (resolved above). No second gate: the selection already
        # expressed intent.
        from omnigent.server.accounts_config import AccountsConfig

        accounts_config = AccountsConfig.from_env()

    return UnifiedAuthProvider(
        source=source,
        oidc_config=oidc_config,
        accounts_config=accounts_config,
    )


# Backwards-compatible re-export of forward-referenced config
# types — both are imported lazily inside `create_auth_provider`
# to keep startup cost off the import path that doesn't use them.
if False:  # TYPE_CHECKING equivalent without the import
    from omnigent.server.accounts_config import AccountsConfig
    from omnigent.server.oidc import OIDCConfig
