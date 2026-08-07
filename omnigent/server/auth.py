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
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING

from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)

# Opt-in multi-user switch.
_AUTH_ENABLED_ENV = "OMNIGENT_AUTH_ENABLED"

RESERVED_USER_LOCAL = "local"
RESERVED_USER_PUBLIC = "__public__"
_RESERVED_USERS = frozenset({RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC})
_TRUTHY_STRINGS = ("1", "true", "yes")

# Path prefixes a delegated (device-grant) access token may reach.
# Fail-closed allowlist: a token carrying a ``scope`` claim is rejected on
# any path not covered here, so it can never touch admin / user-management
# endpoints (``/auth/users``, ``/auth/invite``, ``/auth/setup`` …) even if
# its underlying identity is an admin. Delegated clients only need these.
_DELEGATED_ALLOWED_PREFIXES = (
    "/health",
    "/v1/agents",
    "/v1/hosts",
    "/v1/sessions",
    "/v1/runners",
    "/oauth/token",
    "/oauth/revoke",
)


def delegated_path_allowed(path: str) -> bool:
    """Return True if a delegated access token may access *path*.

    Fail-closed: matches against :data:`_DELEGATED_ALLOWED_PREFIXES` and
    rejects everything else. Exact match or a ``prefix/…`` sub-path
    counts, so ``/v1/hosts`` and ``/v1/hosts/h1/runners`` pass but
    ``/v1/hostsX`` does not.
    """
    for prefix in _DELEGATED_ALLOWED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


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

# Optional prefix stripped from the identity header value in header-auth
# mode. Some trusted proxies namespace the identity they inject — most
# notably Google IAP, whose ``X-Goog-Authenticated-User-Email`` carries an
# ``accounts.google.com:`` prefix (value
# ``accounts.google.com:user@example.com``). Stripping it yields the bare
# email used everywhere else. Unset (the default) strips nothing. See
# :func:`resolve_auth_header_strip_prefix`.
_AUTH_HEADER_STRIP_PREFIX_ENV = "OMNIGENT_AUTH_HEADER_STRIP_PREFIX"

LEVEL_READ = 1
LEVEL_EDIT = 2
LEVEL_MANAGE = 3
LEVEL_OWNER = 4


class SharingMode(str, Enum):
    """Server policy for creating new session permission grants.

    - ``ON``: grants at any level (read/edit/manage) plus workspace/public read.
    - ``READ_ONLY``: grants are capped at read (view) — edit/manage grants are
      rejected; workspace/public read still allowed.
    - ``RESTRICTED_READ_ONLY``: like ``READ_ONLY`` (grants capped at read), but
      sessions whose working directory is a user home directory or the
      filesystem root (see :func:`workspace_sharing_blocked`) cannot be shared
      at all — not even read — because that cwd exposes an entire home/filesystem.
    - ``OFF``: no new grants at all.

    Value is the lowercase name so ``GET /v1/info`` and the
    ``OMNIGENT_SHARING_MODE`` env var round-trip it directly. Defaults to ``ON``.
    """

    OFF = "off"
    READ_ONLY = "read_only"
    RESTRICTED_READ_ONLY = "restricted_read_only"
    ON = "on"

    @classmethod
    def coerce(cls, value: object) -> SharingMode:
        """Map a ``SharingMode``/str/``None`` to a mode, failing open to ``ON``
        for anything unset or unrecognized (env-var parse + callable boundary)."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                return cls.ON
        return cls.ON


# Directories whose *direct children* are user home directories, across the
# Unix / macOS / container layouts a runner might use: ``/home`` (Linux),
# ``/Users`` (macOS), and ``/var/home`` (ostree — Silverblue/CoreOS/Flatcar,
# where ``/home`` symlinks here). Matched by path *shape*, never by resolving
# ``~``: the runner and its home may live on a different host than this server
# process, so the local process's home is not a reliable signal. Deliberately
# excludes project-workspace roots (``/workspace``, ``/workspaces/<repo>``) —
# those hold a single checkout, not a whole home, and stay shareable.
_HOME_PARENT_DIRS = ("/home", "/Users", "/var/home")
# Absolute paths that are themselves a home or the filesystem root.
_BLOCKED_WORKSPACE_ROOTS = ("/", "/root")


def workspace_sharing_blocked(workspace: str | None) -> bool:
    """True when a session's working directory is too broad to share under
    :attr:`SharingMode.RESTRICTED_READ_ONLY` — the filesystem root or a user
    home directory, whose whole contents a grant would expose.

    Recognizes the filesystem root (``/``), root's home (``/root``), and any
    direct child of a common home parent (see :data:`_HOME_PARENT_DIRS` — e.g.
    ``/home/alice``, ``/Users/bob``, ``/var/home/carol``). A subdirectory of a
    home (``/home/alice/proj``) is shareable, as is a ``None``/empty workspace
    (no recorded cwd).

    Pattern-based on purpose: the runner (and thus the home the session lives
    in) may be on a different host than this server process, so only the path
    shape is reliable — resolving the local ``~`` would test the wrong host.
    """
    if not workspace:
        return False
    path = os.path.normpath(workspace)
    if path in _BLOCKED_WORKSPACE_ROOTS:
        return True
    parent, _, leaf = path.rpartition("/")
    return bool(leaf) and parent in _HOME_PARENT_DIRS


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


def resolve_project_owner(user_id: str | None, *, local_single_user: bool) -> str | None:
    """Map a stored "owner or None" column to the identity a project is
    actually owned under, AT THE CURRENT MOMENT this is called.

    Some rows (e.g. ``scheduled_tasks.user_id``) store ``None`` to mean
    "the single local user" regardless of *why* that request had no real
    identity — auth fully disabled (no :class:`AuthProvider` at all) and
    local-single-user mode (an :class:`AuthProvider` configured with
    ``local_single_user=True``, falling back to :data:`RESERVED_USER_LOCAL`
    for headerless requests) both collapse to ``None`` there. But
    :class:`~omnigent.stores.project_store.ProjectStore` rows are owned by
    the RAW identity ``require_user``/``get_user_id`` returned at creation
    (see ``routes/projects.py``'s ``create_project`` and
    ``routes/sessions/routes_core.py``'s project-filing PATCH handler) —
    which is ``None`` when auth is fully disabled, but the literal
    :data:`RESERVED_USER_LOCAL` string in local-single-user mode. This
    reconstructs that raw identity from a ``None``-collapsed owner, using
    whatever auth mode is active RIGHT NOW.

    IMPORTANT — this is only safe to call at the moment a project's
    membership is being ESTABLISHED (create/update, which just validated the
    project under this exact resolution) or as a last-resort fallback for a
    row that predates persisting the resolved owner (see
    :func:`decode_scheduled_task_project_owner`). It is NOT safe to call at
    fire time against an already-created task's stored ``user_id``: the
    server's auth mode can change between a task's creation and a later fire
    (e.g. a restart that flips ``OMNIGENT_LOCAL_SINGLE_USER``), silently
    resolving to the WRONG owner scope and making a still-valid project look
    vanished. The fire path instead reads the owner scope persisted at
    create/update time via ``scheduled_tasks.project_owner`` — see
    :func:`encode_scheduled_task_project_owner` /
    :func:`decode_scheduled_task_project_owner`.

    :param user_id: A stored/normalized owner value — ``None`` for "the
        single local user", or a real user id otherwise.
    :param local_single_user: Whether the server's active auth provider is
        configured for the local-single-user posture (``False`` when auth is
        fully disabled, since there ``None`` already IS the raw identity).
    :returns: The identity a project created under the same conditions is
        actually owned by.
    """
    if user_id is not None:
        return user_id
    return RESERVED_USER_LOCAL if local_single_user else None


# Sentinel stored in ``scheduled_tasks.project_owner`` for "we resolved the
# owner and it really is None" (auth fully disabled). Distinct from SQL NULL,
# which means "this row predates project_owner persistence" (see
# decode_scheduled_task_project_owner). No real identity is ever the empty
# string — emails are non-empty and RESERVED_USER_LOCAL is "local" — so this
# is an unambiguous, collision-free choice.
_PROJECT_OWNER_ANONYMOUS = ""


def encode_scheduled_task_project_owner(owner: str | None) -> str:
    """Encode a resolved project owner (from :func:`resolve_project_owner`)
    for storage in ``scheduled_tasks.project_owner``.

    Call this ONCE, at create/update validation time, right after resolving
    and confirming the project exists under this owner — never at fire time
    (see :func:`resolve_project_owner`'s docstring for why re-resolving there
    is unsafe across an auth-mode change).

    :param owner: The resolved owner — ``None``, :data:`RESERVED_USER_LOCAL`,
        or a real user id.
    :returns: The value to persist. Never ``None`` — a task with no project
        filed persists ``project_owner=None`` (NULL) separately; this
        function is only called when a project IS being filed.
    """
    return owner if owner is not None else _PROJECT_OWNER_ANONYMOUS


def decode_scheduled_task_project_owner(
    stored: str | None, *, user_id: str | None, local_single_user: bool
) -> str | None:
    """Decode a stored ``scheduled_tasks.project_owner`` back to the real
    project-ownership scope, for the fire path's filing lookup.

    ``NULL`` (``stored is None``) means this row predates ``project_owner``
    persistence — a one-time migration gap for tasks with a ``project_id``
    set before this column existed. For those legacy rows only, this falls
    back to re-resolving from the task's normalized ``user_id`` under
    whichever auth mode is active RIGHT NOW (an accepted limitation stated
    explicitly here: a legacy task's project can still look vanished if the
    server's auth mode changed between the task's creation and this
    resolution — the same limitation the feature had before this column was
    added). Every OTHER value — including the empty-string
    :data:`_PROJECT_OWNER_ANONYMOUS` sentinel — is the exact owner resolved
    and persisted at create/update time, immune to any later auth-mode
    change.

    :param stored: The raw ``scheduled_tasks.project_owner`` column value.
    :param user_id: The task's ``user_id``, used only for the legacy
        (``stored is None``) fallback.
    :param local_single_user: The CURRENT server's auth mode, used only for
        the legacy fallback.
    :returns: The project-ownership scope to look the project up under.
    """
    if stored is None:
        return resolve_project_owner(user_id, local_single_user=local_single_user)
    return stored if stored != _PROJECT_OWNER_ANONYMOUS else None


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


def resolve_auth_header_strip_prefix() -> str:
    """Resolve the prefix stripped from the identity header value.

    Reads ``OMNIGENT_AUTH_HEADER_STRIP_PREFIX`` and returns it
    (surrounding whitespace trimmed), or ``""`` when unset or empty —
    the default, meaning the header value is used as-is.

    The motivating case is Google IAP: point
    ``OMNIGENT_AUTH_HEADER=X-Goog-Authenticated-User-Email`` at IAP's
    identity header and set
    ``OMNIGENT_AUTH_HEADER_STRIP_PREFIX=accounts.google.com:`` so the
    namespaced value ``accounts.google.com:user@example.com`` resolves to
    the bare ``user@example.com``. Kept generic rather than IAP-specific
    so any proxy that namespaces its identity header is supported.

    :returns: The prefix to strip, or ``""`` to strip nothing.
    """
    return os.environ.get(_AUTH_HEADER_STRIP_PREFIX_ENV, "").strip()


def _auth_enabled() -> bool:
    """Whether multi-user auth is opted in via the enable switch.

    Reads ``OMNIGENT_AUTH_ENABLED``. The explicit-falsy kill-switch
    semantics mean ``OMNIGENT_AUTH_ENABLED=0`` disables auth even
    though the var is "set", which is how the Docker entrypoint lets an
    operator opt back out of the default-on accounts mode.

    :returns: ``True`` when multi-user auth should be enabled.
    """
    if os.environ.get(_AUTH_ENABLED_ENV, "").strip():
        return env_var_is_truthy(_AUTH_ENABLED_ENV, default=False)
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
      ``OMNIGENT_AUTH_ENABLED`` is truthy (see :func:`_auth_enabled`).
      When enabled, the mode depends on whether OIDC config was
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

    def is_local_single_user(self) -> bool:
        """Whether this provider resolves a headerless request to ``"local"``.

        Default ``False`` (fail-closed posture — a bare request without
        identity is unauthenticated). :class:`UnifiedAuthProvider` overrides
        this to report its own ``local_single_user`` construction flag.
        Used by :func:`resolve_project_owner` — a caller with no auth
        provider at all should pass ``False`` (there is no "local" identity
        concept when auth is fully disabled; a request's identity is simply
        ``None``).
        """
        return False

    def mint_runner_token(self, user_id: str, ttl_seconds: int) -> str | None:  # noqa: ARG002
        """
        Mint a short-lived bearer a managed-sandbox runner presents as *user_id*.

        A managed runner runs in a sandbox with no logged-in user
        credential of its own, so the server mints one for its HTTP
        callbacks when auth is enabled (see the
        ``POST /v1/runners/{id}/token`` endpoint). Default: ``None`` — no
        minting (single-user / no-auth, or a provider whose identity is
        asserted externally and can't be minted server-side, e.g.
        header/proxy auth). The runner then authenticates with its tunnel
        binding token alone.

        :param user_id: The session owner the runner acts as, e.g.
            ``"alice@example.com"``.
        :param ttl_seconds: Token lifetime in seconds.
        :returns: A bearer token string, or ``None`` when this provider
            cannot mint one.
        """
        return None


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
    :param header_strip_prefix: A prefix stripped from the identity
        header value in header mode — e.g. ``accounts.google.com:`` so
        Google IAP's ``accounts.google.com:user@example.com`` resolves
        to the bare email. ``None`` (the default) resolves from
        ``OMNIGENT_AUTH_HEADER_STRIP_PREFIX`` at construction, falling
        back to ``""`` (strip nothing; see
        :func:`resolve_auth_header_strip_prefix`). Only consulted in
        header mode. Tests pass an explicit prefix.
    """

    def __init__(
        self,
        source: str,
        oidc_config: OIDCConfig | None = None,
        accounts_config: AccountsConfig | None = None,
        local_single_user: bool | None = None,
        header_name: str | None = None,
        header_strip_prefix: str | None = None,
    ) -> None:
        self._source = source
        self._oidc_config = oidc_config
        self._accounts_config = accounts_config
        self._local_single_user = (
            local_single_user if local_single_user is not None else local_single_user_enabled()
        )
        self._header_name = header_name if header_name is not None else resolve_auth_header()
        self._header_strip_prefix = (
            header_strip_prefix
            if header_strip_prefix is not None
            else resolve_auth_header_strip_prefix()
        )
        self._cookie_cache: dict[str, tuple[str, float]] = {}
        # Set by create_app when a device-grant store is wired. Returns
        # True if a grant_id has been revoked (or is unknown → fail
        # closed). Consulted only for delegated tokens (those carrying a
        # ``grant_id`` claim); left None disables the check.
        self._grant_revoked: Callable[[str], bool] | None = None

    def set_grant_revocation_check(self, check: Callable[[str], bool]) -> None:
        """Wire the device-grant revocation lookup.

        :param check: Callable mapping a ``grant_id`` to True when the
            grant is revoked or unknown (fail closed).
        """
        self._grant_revoked = check

    def is_local_single_user(self) -> bool:
        """Whether a headerless request on THIS provider resolves to ``"local"``.

        The ``local_single_user`` constructor flag is only consulted by
        ``_check_header`` in ``"header"`` mode (see the class docstring); an
        ``"oidc"``/``"accounts"`` provider always requires a real cookie
        session regardless of the flag's value, so this is source-aware
        rather than echoing the raw flag — a provider constructed with
        ``source="oidc", local_single_user=True`` (an unusual combination,
        but not rejected at construction) must NOT report ``True`` here, or
        callers like :func:`resolve_project_owner` would resolve a project
        owner scope this provider's requests can never actually produce.
        """
        return self._source == "header" and self._local_single_user

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

    def mint_runner_token(self, user_id: str, ttl_seconds: int) -> str | None:
        """
        Mint a short-lived owner JWT for a managed-sandbox runner.

        Accounts / OIDC modes sign a session JWT in the same HS256 format
        :meth:`_check_cookie` validates, so the runner can present it as
        ``Authorization: Bearer <jwt>`` on its HTTP callbacks and resolve
        to *user_id*. Header/proxy mode returns ``None`` — identity there
        is asserted by the upstream proxy and can't be minted server-side.

        :param user_id: The session owner the runner acts as, e.g.
            ``"alice@example.com"``.
        :param ttl_seconds: Token lifetime in seconds.
        :returns: An HS256-signed JWT, or ``None`` for header mode, an
            empty/reserved user, or a missing cookie config.
        """
        if not user_id or user_id in _RESERVED_USERS:
            return None
        if self._source not in ("oidc", "accounts"):
            return None
        cookie_config = self._oidc_config if self._source == "oidc" else self._accounts_config
        if cookie_config is None:
            return None
        from omnigent.server.oidc import mint_session_token

        return mint_session_token(
            user_id,
            cookie_config.cookie_secret,
            ttl_seconds,
            self._source,
        )

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
        if cookie_config is None:
            return None
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
        if not isinstance(user_id, str) or not user_id or user_id in _RESERVED_USERS:
            return None

        # Delegated (device-grant) tokens carry a ``grant_id`` claim.
        # They get two extra, request-scoped checks — a fail-closed path
        # allowlist and a live revocation lookup — so they are never
        # served from the plain user-id cache (which would skip both).
        grant_id = payload.get("grant_id")
        if grant_id is not None:
            if not isinstance(grant_id, str):
                return None
            if not delegated_path_allowed(request.url.path):
                return None
            if self._grant_revoked is not None and self._grant_revoked(grant_id):
                return None
            return user_id

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

        When :attr:`_header_strip_prefix` is set (from
        ``OMNIGENT_AUTH_HEADER_STRIP_PREFIX``), it is removed from the
        front of the header value first — e.g. Google IAP's
        ``X-Goog-Authenticated-User-Email`` value
        ``accounts.google.com:user@example.com`` becomes the bare
        ``user@example.com``. A value that is only the prefix (empty
        after stripping) is rejected, like a reserved name.

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
            if self._header_strip_prefix:
                email = email.removeprefix(self._header_strip_prefix)
            if not email or email in _RESERVED_USERS:
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

    (``OMNIGENT_AUTH_ENABLED`` is the opt-in gate: header is the
    shipped default, so the var is an enable switch, not a kill switch.)

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
if TYPE_CHECKING:
    from omnigent.server.accounts_config import AccountsConfig
    from omnigent.server.oidc import OIDCConfig
