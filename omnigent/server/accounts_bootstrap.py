"""First-boot ``admin`` user provisioning for the accounts auth provider.

Run once at server startup when ``OMNIGENT_AUTH_PROVIDER=accounts``.

**No default credentials, ever.** We never auto-generate a password.
The first admin is claimed by choosing a username + password through
exactly one of three paths, all of which set the same credential:

1. **Password flag** — ``--admin-password`` /
   ``OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD``. Headless / CI. Bootstrap
   creates the admin from it directly (this module).
2. **Terminal prompt** — ``omnigent server`` on a TTY asks for a
   username + password before serving (``cli.py``).
3. **Web Create-admin form** — when neither of the above applied,
   bootstrap creates nothing and reports ``needs_setup``; the SPA then
   shows a "Create admin" form that POSTs ``/auth/setup`` (gated to
   the zero-admin state). On a loopback boot the lifespan auto-opens
   the browser to that form.

This sidesteps the random-password footgun (a credential buried in
container logs the operator can't reach on Render/Railway) AND the
unauth setup-token CVE class (Metabase CVE-2023-38646): ``/auth/setup``
holds no secret and self-disables the instant any account exists.

Idempotency: if an admin already exists, the function is a no-op (a
supplied password is ignored with a warning). Rotation is an explicit
action — Account menu → Change password, or admin Members → Reset.

**Loopback CLI handoff.** On a loopback boot, once an admin exists,
bootstrap writes a session JWT to ``~/.omnigent/auth_tokens.json``
(via :mod:`omnigent.cli_auth`) keyed to this spawn's URL, so the next
``omnigent run`` is signed in without a prompt. Skipped for
non-loopback (remote) deploys where the server's machine ≠ the
operator's.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.auth import _RESERVED_USERS
from omnigent.server.passwords import hash_password

logger = logging.getLogger(__name__)

# Fallback when neither the env override nor the OS username is
# usable as an account name. Matches the GitLab / Gitea convention
# so docs and password-recovery flows have something stable to
# reference.
_ADMIN_USERNAME_FALLBACK = "admin"

# Username regex — duplicated from ``routes/accounts_auth.py`` to
# avoid an import cycle (routes already import from this module).
# Keep in sync with that file's ``_USERNAME_RE``.
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(@[a-z0-9.-]+\.[a-z]{2,})?$")


def resolve_admin_username() -> str:
    """Pick the username for the first-boot admin.

    Resolution order:

    1. ``OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME`` env var — explicit
       operator override. Useful for headless / Docker deploys where
       ``getpass.getuser()`` would return something unhelpful like
       ``"root"`` or where the deploy wants a stable name.
    2. ``getpass.getuser()`` — the OS user running the server. On
       a laptop, this is the operator's own login (``"dhruv.gupta"``,
       ``"alice"``, etc.) so the CLI's auto-handoff JWT and the
       web UI's admin row are the same identity from the start —
       no separate "local" / "admin" split.
    3. Literal ``"admin"`` — fallback when (a) #1 isn't set, (b)
       #2 raises, returns a reserved name, or returns a value that
       doesn't satisfy the username regex (e.g. contains uppercase
       or characters the route rejects).

    Sanitization: lowercase, strip; reserved names (``"local"``,
    ``"__public__"``) fall through to the fallback so the bootstrap
    can't accidentally collide with the header-mode sentinel.
    """
    explicit = os.environ.get("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", "").strip()
    if explicit:
        return _sanitize_admin_username(explicit) or _ADMIN_USERNAME_FALLBACK

    try:
        os_user = getpass.getuser()
    except Exception:  # noqa: BLE001 — getpass can raise on weird shells / no pwd
        return _ADMIN_USERNAME_FALLBACK

    return _sanitize_admin_username(os_user) or _ADMIN_USERNAME_FALLBACK


def _sanitize_admin_username(candidate: str) -> str | None:
    """Lowercase + validate against the route regex.

    Returns ``None`` when the value can't be used — the caller
    falls back to ``"admin"``. Reserved names also return None
    so they don't pass through silently.
    """
    norm = candidate.strip().lower()
    if not norm or norm in _RESERVED_USERS:
        return None
    if not _USERNAME_RE.fullmatch(norm):
        return None
    return norm


@dataclass(frozen=True)
class BootstrapResult:
    """What :func:`bootstrap_admin` accomplished on this boot.

    Returned to ``create_app`` → lifespan startup so it can do the
    deferred parts that need uvicorn to have bound the port first —
    chiefly auto-opening the browser to the first-run setup form.

    :param fresh_boot: ``True`` if this call created the admin row
        (only the ``INIT_ADMIN_PASSWORD`` / ``--admin-password`` path
        does that now). ``False`` on a re-boot or a needs-setup boot.
    :param needs_setup: ``True`` when no admin exists yet and none was
        created this boot (no password was supplied) — so the first
        admin must be claimed via the web Create-admin form, the
        ``omnigent server`` terminal prompt, or a password flag.
        Drives ``/v1/info``'s ``needs_setup`` and the browser auto-open.
    :param open_url: URL the lifespan should auto-open once bound, or
        ``None``. Set to the loopback base URL on a needs-setup local
        boot so the browser lands on the Create-admin form.
    :param tui_token_written: ``True`` when a CLI session JWT was
        written to ``~/.omnigent/auth_tokens.json`` (loopback only),
        so the next ``omnigent run`` is signed in without a prompt.
    """

    fresh_boot: bool
    needs_setup: bool
    open_url: str | None
    tui_token_written: bool


def _is_loopback_base_url(base_url: str) -> bool:
    """Whether ``base_url`` looks like a laptop / local deployment.

    True for ``http://localhost:*`` and ``http://127.0.0.1:*``
    (also ``[::1]``). The handoff side-effects (writing into the
    user's home dir, auto-opening the browser) only make sense
    when the server is running on the same machine as the
    operator's UI.
    """
    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def _local_admin_username(account_store: SqlAlchemyAccountStore) -> str | None:
    """Pick the admin to auto-sign-in as on a loopback server.

    A loopback, daemon-spawned server is single-user-local: the
    machine operator is its admin. Prefer the deterministic
    OS-user-derived name (:func:`resolve_admin_username`, the one a
    fresh bootstrap would have created) when it's a password-having
    admin; otherwise fall back to the first such admin. ``None`` when
    no password-having admin exists yet (the create-admin path hasn't
    run).

    :param account_store: The accounts store to read users from.
    :returns: The admin's user id, e.g. ``"dhruv.gupta"``, or ``None``.
    """
    admins = [u for u in account_store.list_users() if u.is_admin and u.has_password]
    resolved = resolve_admin_username()
    for u in admins:
        if u.id == resolved:
            return u.id
    return admins[0].id if admins else None


def _mint_loopback_cli_token(
    admin_username: str,
    *,
    base_url: str,
    cookie_secret: bytes,
    session_ttl_hours: int,
) -> bool:
    """Write a fresh CLI session token for a loopback server spawn.

    The CLI authenticates to a server by a token in
    ``~/.omnigent/auth_tokens.json`` keyed by the **exact** server
    URL. The daemon spawns the local server on a fresh random port
    every time, so a token minted at first-boot (and keyed to that
    boot's port) won't match a later spawn — and on a returning boot
    the first-boot handoff never re-fires at all. Without re-minting
    per spawn, ``omnigent run`` 401s against its own loopback
    server once an admin already exists. This mints a token for
    ``base_url`` (the current spawn's URL) on every boot, so the
    local CLI always has a valid credential. Loopback + single-user,
    so auto-signing-in the machine operator is safe.

    Best-effort: a failure (read-only fs, no HOME) logs a warning and
    returns ``False`` rather than blocking server boot.

    :param admin_username: The user id to mint the token for, e.g.
        ``"dhruv.gupta"``.
    :param base_url: This spawn's server URL, e.g.
        ``"http://127.0.0.1:54312"``.
    :param cookie_secret: HMAC key for the HS256 session JWT.
    :param session_ttl_hours: Token lifetime in hours, e.g. ``8``.
    :returns: ``True`` if the token was written, ``False`` otherwise.
    """
    try:
        from omnigent import cli_auth
        from omnigent.server.oidc import mint_session_cookie

        now = int(time.time())
        tui_jwt = mint_session_cookie(
            user_id=admin_username,
            cookie_secret=cookie_secret,
            ttl_hours=session_ttl_hours,
            provider="accounts",
        )
        cli_auth.store_token(
            server_url=base_url,
            token=tui_jwt,
            user_id=admin_username,
            expires_at=now + session_ttl_hours * 3600,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, must not block boot
        logger.warning(
            "accounts: failed to write loopback CLI token (%s) — "
            "`omnigent run` may need `omnigent login`",
            exc,
        )
        return False


def bootstrap_admin(
    account_store: SqlAlchemyAccountStore,
    *,
    init_admin_password: str | None = None,
    base_url: str | None = None,
    session_ttl_hours: int = 8,
    cookie_secret: bytes | None = None,
) -> BootstrapResult:
    """Ensure an ``admin`` user exists; create + announce on first boot.

    Behavior matrix:

    - ``admin`` already exists → no-op. Returns
      ``BootstrapResult(fresh_boot=False, ...)``.
    - ``admin`` missing, ``init_admin_password`` provided →
      creates with that password. Skips the auto-handoff entirely
      (the operator already knows the credential and wants the
      standard typed-login flow).
    - ``admin`` missing, no pre-seed → creates nothing and reports
      ``needs_setup=True`` so the operator claims the first admin
      through the web Create-admin form (``POST /auth/setup``) or a
      terminal prompt. No password is ever auto-generated. On a
      loopback base URL, ``open_url`` is set so the lifespan opens
      the browser to that form; otherwise the "no admin yet" URL is
      printed to stderr.

    Ordering (pre-seed path only): the user row is persisted BEFORE
    any loopback handoff tokens so a token can never reference a
    non-existent user.

    :param account_store: The accounts-specific store backing user
        identity + tokens. Mutually exclusive with PermissionStore
        for accounts concerns — see ``omnigent/server/accounts_store.py``
        for the boundary rationale.
    :param init_admin_password: Optional operator-provided
        password from ``OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD``.
    :param base_url: The server's public base URL — used for the
        loopback check (which gates the CLI handoff + browser
        auto-open) and as the URL to open on a needs-setup local
        boot. ``None`` disables those.
    :param session_ttl_hours: TTL for the CLI session token
        written to ``~/.omnigent/auth_tokens.json``.
    :param cookie_secret: HMAC key for the session JWT. ``None``
        also disables the handoff (no way to mint a valid token).
    :returns: A :class:`BootstrapResult` describing what fired.
    """
    # "Fresh boot" used to be ``get_user("admin") is None`` but now
    # the admin's name defaults to the OS user (resolve_admin_username),
    # so the gate has to be "is there any password-having user yet?"
    # ``list_users()`` already filters the legacy ``"local"`` row +
    # the ``"__public__"`` sentinel, so this counts real accounts only.
    if any(u.has_password for u in account_store.list_users()):
        # An admin password is set exactly once, on the first boot of a
        # machine's accounts DB. If the operator passed a fresh password
        # (--admin-password / INIT_ADMIN_PASSWORD) on a DB that's already
        # initialized, don't silently apply or silently drop it — warn and
        # name where the credential record lives so they know it was a
        # no-op and how to rotate instead.
        if init_admin_password:
            # Print to stderr (not just logger.warning) — the operator
            # explicitly passed a password and needs to SEE that it was a
            # no-op. A logger.warning at server startup is easily missed.
            print(
                "\n  ⚠ admin already exists — ignoring the supplied password\n"
                "    (--admin-password / OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD).\n"
                "    To change it: sign in, then Account menu → Change password\n"
                "    (or, as an admin, Members → Reset for another user).\n",
                file=sys.stderr,
            )
        else:
            logger.info("accounts: at least one account user already exists, skipping bootstrap")
        # Bootstrap is a no-op here, but the loopback CLI still needs a
        # fresh token for THIS spawn's port — the daemon picks a new port
        # each spawn and the first-boot handoff token is port-keyed +
        # one-time. Without this, `omnigent run` 401s against its own
        # local server once an admin exists.
        refreshed = False
        if base_url is not None and cookie_secret is not None and _is_loopback_base_url(base_url):
            local_admin = _local_admin_username(account_store)
            if local_admin is not None:
                refreshed = _mint_loopback_cli_token(
                    local_admin,
                    base_url=base_url,
                    cookie_secret=cookie_secret,
                    session_ttl_hours=session_ttl_hours,
                )
        return BootstrapResult(
            fresh_boot=False, needs_setup=False, open_url=None, tui_token_written=refreshed
        )

    # ── No admin yet ──────────────────────────────────────────────
    # We NEVER auto-generate a password (no default credentials, ever).
    # The first admin is claimed by choosing a username + password
    # through one of three paths:
    #   1. a password flag (handled right below): --admin-password /
    #      OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD — headless / CI;
    #   2. the `omnigent server` terminal prompt (cli.py, on a TTY);
    #   3. the web Create-admin form (POST /auth/setup), which the SPA
    #      shows whenever /v1/info reports needs_setup.
    # When no password was supplied we create nothing and report
    # needs_setup so paths 2/3 can drive it.
    if not init_admin_password:
        # Defer to the terminal prompt / web form. On a loopback boot,
        # ask the lifespan to open the browser to the setup form.
        open_url = base_url if (base_url is not None and _is_loopback_base_url(base_url)) else None
        if open_url is None and base_url is not None:
            print(
                f"\n  → No admin yet. Open {base_url.rstrip('/')} to create the "
                "first admin account (choose a username + password).\n",
                file=sys.stderr,
            )
        return BootstrapResult(
            fresh_boot=False, needs_setup=True, open_url=open_url, tui_token_written=False
        )

    admin_username = resolve_admin_username()
    # Multi-worker race: two workers can both pass the check above and
    # call create_user_with_password; the loser's ValueError is the
    # idempotency path. Both supplied the same INIT password, so there's
    # no asymmetry.
    try:
        account_store.create_user_with_password(
            admin_username,
            hash_password(init_admin_password),
            is_admin=True,
        )
    except ValueError:
        logger.info("accounts: %r already created by another worker, skipping", admin_username)
        return BootstrapResult(
            fresh_boot=False, needs_setup=False, open_url=None, tui_token_written=False
        )
    logger.info("accounts: created %r admin from supplied password", admin_username)

    # Loopback CLI handoff so `omnigent run` is signed in without a
    # prompt (keyed to this spawn's URL; see _mint_loopback_cli_token).
    tui_token_written = False
    if base_url is not None and cookie_secret is not None and _is_loopback_base_url(base_url):
        tui_token_written = _mint_loopback_cli_token(
            admin_username,
            base_url=base_url,
            cookie_secret=cookie_secret,
            session_ttl_hours=session_ttl_hours,
        )

    return BootstrapResult(
        fresh_boot=True, needs_setup=False, open_url=None, tui_token_written=tui_token_written
    )
