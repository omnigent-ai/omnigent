"""Configuration for the ``accounts`` auth provider.

Mirrors the cookie + session bits of :class:`omnigent.server.oidc.OIDCConfig`
so the same ``__Host-ap_session`` JWT machinery in :class:`UnifiedAuthProvider`
can serve both providers without branching on every cookie read.

The split between OIDCConfig and AccountsConfig is intentional —
OIDC has IdP-specific knobs (issuer, client_id, client_secret,
redirect_uri, scopes, allowed_domains, discovery) that accounts
mode has no concept of. Both share only the cookie / session
parameters, which are extracted here as the common shape.

Env vars (all start with ``OMNIGENT_ACCOUNTS_``):

- ``ENABLED`` — required, must be truthy (``1`` / ``true`` /
  ``yes``). Enforced by :func:`create_auth_provider` — a
  defense-in-depth second gate beyond
  ``OMNIGENT_AUTH_PROVIDER=accounts`` so a fat-finger on the
  provider env var can't accidentally activate accounts mode on
  a deployment the operator thinks is still in header/OIDC.
  Accounts is experimental in v1; this gate goes away once it
  flips to default.
- ``COOKIE_SECRET`` — required, 64+ hex chars. HMAC key for HS256
  session cookies. Generate with ``openssl rand -hex 32`` (or
  ``deploy/docker/bootstrap.sh`` mints one alongside POSTGRES_PASSWORD).
- ``SESSION_TTL_HOURS`` — optional, default 8. The browser session's idle
  limit, measured from the user's last interaction with the web UI
  (pointer, click, key, mouse wheel or touch input, or the window or tab
  being reactivated; loading a page is not one). The UI reports the time
  since then on its API requests (``X-Omnigent-Activity-Age``); a
  cookie-authenticated request made after half the cookie's lifetime gets
  a fresh cookie expiring this long after that interaction (sliding
  renewal). Background polling, automatic scrolling and focus changes
  don't count, so an open but unattended tab signs out on schedule; a
  closed tab sends nothing and is signed out no later than this long after
  its last interaction, possibly sooner. Idle is measured per browser, not
  per tab: all tabs share the session cookie and (through local storage)
  the last-interaction time, so activity in any tab keeps it alive, and
  the limit runs from the last interaction once every tab is idle.
  Requests without the header never renew. The idle time is
  client-reported: code holding the cookie could fake it, as it could keep
  a session alive with requests anyway, so this protects an unattended
  screen, not a compromised device.
- ``SESSION_MAX_LIFETIME_HOURS`` — optional, default 720 (30 days, or
  ``SESSION_TTL_HOURS`` if that is longer). The absolute limit on a
  browser session: renewal never extends it past the original login
  plus this, after which the user signs in again. 30 days matches the
  usual "keep me signed in" horizon — a daily user re-enters their
  password about once a month, while an idle session still ends
  ``SESSION_TTL_HOURS`` after its last interaction. It is the hard cap on
  every session, however active. Must be at least ``SESSION_TTL_HOURS``;
  set it equal to disable sliding renewal. Session cookies minted before renewal
  existed carry no ``auth_time`` claim and are never renewed — they
  lapse at their original expiry and the next login mints a renewable
  one. Only the session cookie slides; ``Authorization: Bearer`` tokens
  and delegated tokens (``grant_id`` / ``scope``) keep their fixed
  expiry and renew through their refresh grants instead. Renewal needs
  the database-backed logout record, so a ``create_app`` without a
  ``permission_store`` never renews (see
  :meth:`UnifiedAuthProvider.set_session_revocation_store`).
- ``BASE_URL`` — required. The user-facing base URL of the
  deployment, e.g. ``"https://omnigent.example.com"`` or
  ``"http://localhost:6767"``. Determines whether session cookies
  use the secure ``__Host-`` prefix. We require this explicitly
  (rather than inferring per-request from the Host header) so the
  cookie attributes are stable across reverse proxies.
- ``INIT_ADMIN_PASSWORD`` — optional. When set on first boot,
  bootstrap creates the ``admin`` user directly with this password.
  No credential is ever auto-generated: when it's unset the first
  admin is claimed instead through the web Create-admin form (or a
  terminal prompt). Useful for headless deploys (CI, Cloud Run,
  etc.) where that form can't be reached interactively.
- ``INVITE_TTL_HOURS`` — optional, default 72. Invite tokens
  expire after this window.
- ``MAGIC_TTL_MINUTES`` — optional, default 10. Magic-link
  tokens are intentionally very short-lived since they're used
  for an immediate CLI→browser handoff.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from omnigent.server.oidc import (
    DEFAULT_SESSION_MAX_LIFETIME_HOURS,
    parse_session_max_lifetime_hours,
)


@dataclass(frozen=True)
class AccountsConfig:
    """Validated accounts-auth configuration, built once at startup.

    :param cookie_secret: HMAC-SHA256 key bytes for HS256 session
        cookies. Decoded from hex; minimum 32 bytes (64 hex chars).
    :param session_ttl_hours: The browser session's idle limit in
        hours, measured from the user's last interaction; past half the
        cookie's lifetime it is renewed to expire this long after it.
    :param base_url: Public base URL of the deployment. Drives
        the ``Secure`` cookie attribute and the ``__Host-`` prefix.
    :param init_admin_password: Optional pre-seeded password for
        the first-boot admin user. ``None`` means no admin is
        created at boot — the first one is claimed via the web
        Create-admin form (nothing is auto-generated).
    :param invite_ttl_seconds: How long ``/auth/invite`` tokens
        are redeemable for.
    :param magic_ttl_seconds: How long ``/auth/magic`` tokens
        are redeemable for. Short by design.
    :param session_max_lifetime_hours: Absolute browser-session
        lifetime in hours. Sliding renewal never extends a session
        past its login plus this.
    """

    cookie_secret: bytes
    session_ttl_hours: int
    base_url: str
    init_admin_password: str | None
    invite_ttl_seconds: int
    magic_ttl_seconds: int
    session_max_lifetime_hours: int = DEFAULT_SESSION_MAX_LIFETIME_HOURS

    @property
    def session_max_lifetime_seconds(self) -> int:
        """Absolute browser-session lifetime, never below the idle window."""
        return max(self.session_max_lifetime_hours, self.session_ttl_hours) * 3600

    @property
    def secure_cookies(self) -> bool:
        """Whether cookies should set ``Secure`` and use ``__Host-``.

        ``True`` for HTTPS base URLs, ``False`` for plain HTTP
        (local dev). The ``__Host-`` prefix requires HTTPS;
        browsers silently drop the cookie on HTTP, which would
        cause an infinite login redirect.
        """
        return self.base_url.startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        """The cookie name used for session JWTs.

        Uses the ``__Host-`` prefix on HTTPS (prevents subdomain
        cookie-tossing attacks) and a plain name on HTTP local dev.
        """
        return "__Host-ap_session" if self.secure_cookies else "ap_session"

    @staticmethod
    def from_env() -> AccountsConfig:
        """Read and validate every required ``OMNIGENT_ACCOUNTS_*`` env var.

        :returns: A validated :class:`AccountsConfig`.
        :raises RuntimeError: On any missing or malformed value.
            Errors are written in the same fail-loud style as
            :meth:`OIDCConfig.from_env`.
        """

        def _require(name: str) -> str:
            val = os.environ.get(name, "").strip()
            if not val:
                raise RuntimeError(
                    f"Missing required environment variable {name} "
                    f"(OMNIGENT_AUTH_PROVIDER=accounts requires it)"
                )
            return val

        cookie_secret_hex = _require("OMNIGENT_ACCOUNTS_COOKIE_SECRET")
        try:
            cookie_secret = bytes.fromhex(cookie_secret_hex)
        except ValueError as exc:
            raise RuntimeError(
                "OMNIGENT_ACCOUNTS_COOKIE_SECRET must be a valid hex string"
            ) from exc
        if len(cookie_secret) < 32:
            raise RuntimeError(
                "OMNIGENT_ACCOUNTS_COOKIE_SECRET must be at least 32 bytes "
                "(64 hex chars). Generate with `openssl rand -hex 32`."
            )

        base_url = _require("OMNIGENT_ACCOUNTS_BASE_URL").rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise RuntimeError(
                f"OMNIGENT_ACCOUNTS_BASE_URL must start with http:// or https://; got {base_url!r}"
            )

        session_ttl_hours = int(os.environ.get("OMNIGENT_ACCOUNTS_SESSION_TTL_HOURS", "8"))
        session_max_lifetime_hours = parse_session_max_lifetime_hours(
            "OMNIGENT_ACCOUNTS_SESSION_MAX_LIFETIME_HOURS", session_ttl_hours
        )
        invite_ttl_seconds = int(os.environ.get("OMNIGENT_ACCOUNTS_INVITE_TTL_HOURS", "72")) * 3600
        magic_ttl_seconds = int(os.environ.get("OMNIGENT_ACCOUNTS_MAGIC_TTL_MINUTES", "10")) * 60

        # INIT_ADMIN_PASSWORD: explicit empty string ("") is treated as
        # unset for the same reason as the OIDC SCOPES fix
        # — docker compose ${VAR:-} pattern forwards the var as set-to-"".
        init_admin = os.environ.get("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD") or None

        return AccountsConfig(
            cookie_secret=cookie_secret,
            session_ttl_hours=session_ttl_hours,
            base_url=base_url,
            init_admin_password=init_admin,
            invite_ttl_seconds=invite_ttl_seconds,
            magic_ttl_seconds=magic_ttl_seconds,
            session_max_lifetime_hours=session_max_lifetime_hours,
        )
