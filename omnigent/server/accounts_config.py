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
- ``SESSION_TTL_HOURS`` — optional, default 8. How long a
  ``/auth/login`` cookie stays valid.
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


@dataclass(frozen=True)
class AccountsConfig:
    """Validated accounts-auth configuration, built once at startup.

    :param cookie_secret: HMAC-SHA256 key bytes for HS256 session
        cookies. Decoded from hex; minimum 32 bytes (64 hex chars).
    :param session_ttl_hours: How many hours a login cookie is
        valid.
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
    """

    cookie_secret: bytes
    session_ttl_hours: int
    base_url: str
    init_admin_password: str | None
    invite_ttl_seconds: int
    magic_ttl_seconds: int

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
        )
