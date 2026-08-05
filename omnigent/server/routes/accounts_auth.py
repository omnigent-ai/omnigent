"""Accounts auth routes: login, logout, me, invite, register, magic-link, members admin.

Mounted at ``/auth`` when ``OMNIGENT_AUTH_PROVIDER=accounts``.
Mutually exclusive with the OIDC auth router (which mounts at the
same prefix for OIDC mode) — only one is wired by ``create_app``.

The shape mirrors what the field has converged on for self-hosted
projects with built-in accounts (Immich / Gitea / n8n / Coolify /
Plausible — see ``designs/oss-cuj/01-research-summary.md`` §2.2.1):

- Login / logout via username + password (one cookie set on success).
- Invite-only signup — an admin mints a single-use copyable URL.
- No SMTP required — invite + admin-initiated reset cover the
  "I forgot my password" path.
- A short-TTL magic-link endpoint so the CLI can hand a signed-in
  session to the browser without the user typing their password
  on the laptop a second time.

Cookie machinery is shared verbatim with the OIDC router (same
HS256 ``__Host-ap_session`` JWT). The only difference is what
mints the token — here it's ``/auth/login`` validating a stored
password hash, not an IdP authorization code exchange.
"""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, RedirectResponse, Response

from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.admin_list import AdminList, promote_if_listed
from omnigent.server.auth import _RESERVED_USERS, RESERVED_USER_LOCAL, UnifiedAuthProvider
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.passwords import (
    InvalidPasswordError,
    hash_password,
    needs_rehash,
    verify_password,
)
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# Minimum password length enforced at the route layer. Matches
# NIST SP 800-63B §5.1.1.2 ("memorized secrets shall be at least
# 8 characters"). We don't enforce composition rules (mix of
# uppercase / digits / etc.) — current NIST guidance is to drop
# composition rules in favor of length + denylist.
_MIN_PASSWORD_LENGTH = 8

# Username constraints. Lowercase letters, digits, hyphens, dots,
# underscores; 1-64 chars. Restrictive on purpose — narrow the
# attack surface for log injection, terminal escape sequences,
# email-impersonation tricks. Real emails are also accepted (the
# @ + dot pattern matches the regex).
_USERNAME_RE = r"^[a-z0-9][a-z0-9._-]{0,63}(@[a-z0-9.-]+\.[a-z]{2,})?$"


class LoginRequest(BaseModel):
    """Body of ``POST /auth/login``.

    :param username: The user's chosen username, lowercased
        before lookup.
    :param password: Plaintext password, length-validated against
        :data:`_MIN_PASSWORD_LENGTH` but otherwise opaque.
    """

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class RegisterRequest(BaseModel):
    """Body of ``POST /auth/register``.

    :param invite: The invite token from the copyable URL.
    :param username: The user's chosen username.
    :param password: The user's chosen password.
    """

    invite: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=64, pattern=_USERNAME_RE)
    password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=1024)


class SetupRequest(BaseModel):
    """Body of ``POST /auth/setup`` (first-run web admin claim).

    :param username: The chosen admin username, e.g. ``"alice"``.
        Same charset rules as registration.
    :param password: The chosen admin password.
    """

    username: str = Field(min_length=1, max_length=64, pattern=_USERNAME_RE)
    password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=1024)


class InviteRequest(BaseModel):
    """Body of ``POST /auth/invite``.

    :param is_admin: Whether the resulting user should be created
        with admin rights. Defaults False; only admins can mint
        invites at all (route-level guard), and admin invites are
        a deliberate sub-case.
    """

    is_admin: bool = False


class ChangePasswordRequest(BaseModel):
    """Body of ``POST /auth/users/me/password``.

    :param old_password: Current password, verified before the
        change takes effect.
    :param new_password: Replacement password.
    """

    old_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=_MIN_PASSWORD_LENGTH, max_length=1024)


def _set_session_cookie(
    response: Response,
    token: str,
    *,
    cookie_name: str,
    secure: bool,
    max_age_seconds: int,
) -> None:
    """Attach the session JWT to a response.

    Centralized so every cookie-setting site (login, register,
    magic redeem) uses the same attributes — divergence here is
    a recipe for "works in one route but not another" auth bugs.
    """
    # samesite="lax" is the right CSRF-safe default for a standalone deploy
    # (top-level navigation to the app's own domain). It does NOT work when the
    # app is embedded in a cross-origin iframe — e.g. Hugging Face Spaces' preview
    # pane — because browsers won't send a Lax cookie in a third-party frame, so
    # login appears to loop. The validated workaround is to open the app at its
    # direct URL (top-level tab). To support the embedded case, this would need a
    # "samesite=none; Secure" option gated behind an opt-in env var — deferred, as
    # it widens CSRF exposure and the direct-URL path already works.
    response.set_cookie(
        key=cookie_name,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response, *, cookie_name: str, secure: bool) -> None:
    """Delete the session cookie. Same attrs as the setter, by design."""
    response.delete_cookie(
        key=cookie_name,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _validate_username(username: str) -> str | None:
    """Normalize + check a username; return error message or None.

    Lowercases, strips, then rejects reserved names + non-matching
    pattern. Reserved-name rejection here is belt-and-suspenders
    — the auth provider also rejects them at cookie validation
    time so the constraint can't be bypassed even if a row sneaks
    in via direct SQL.
    """
    norm = username.strip().lower()
    if norm in _RESERVED_USERS:
        return f"username {norm!r} is reserved"
    if not re.fullmatch(_USERNAME_RE, norm):
        return (
            "username must be lowercase letters / digits / "
            "dots / hyphens / underscores (or a lowercase email)"
        )
    return None


def _redact_for_log(user_id: str) -> str:
    """Truncate a user id for log lines.

    Logs are aggregated centrally and we don't want full emails
    landing in DataDog / journald. Show prefix + length so a
    real incident can correlate without grepping the full PII.
    """
    if len(user_id) <= 4:
        return f"{user_id[:1]}***"
    return f"{user_id[:3]}***(len={len(user_id)})"


def create_accounts_auth_router(
    auth_provider: UnifiedAuthProvider,
    account_store: SqlAlchemyAccountStore,
    admin_list: AdminList,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the ``/auth/*`` router for the accounts provider.

    :param auth_provider: Must have ``_source == 'accounts'`` and
        ``_accounts_config`` populated.
    :param account_store: Accounts-specific persistence (user
        rows + invite/magic tokens). Sibling to PermissionStore;
        accounts routes never touch the latter so the
        PermissionStore interface stays stable for the internal
        hosted product. Required — there's no header fallback in
        accounts mode.
    :param admin_list: File-backed admin roster, consulted on each
        successful login/register/magic redeem to promote a listed
        username to admin (additive — see
        :mod:`omnigent.server.admin_list`).
    :param permission_store: Optional — used ONLY on a loopback
        single-user server's first-run ``/auth/setup`` to migrate the
        reserved ``local`` user's session grants to the new admin (so
        pre-accounts chats stay visible). ``None`` (and on non-loopback
        deploys) leaves session permissions untouched, preserving the
        accounts-routes-don't-touch-PermissionStore boundary for the
        hosted product.
    :returns: APIRouter to mount at ``/auth``.
    """
    if auth_provider._source != "accounts":
        raise RuntimeError("create_accounts_auth_router called with non-accounts provider")
    config = auth_provider._accounts_config
    if config is None:
        raise RuntimeError("accounts auth provider has no accounts configuration")
    router = APIRouter()

    _secure = config.secure_cookies
    _session_cookie = config.session_cookie_name
    _session_max_age = config.session_ttl_hours * 3600

    # ── Login / logout / me ───────────────────────────────────────

    @router.post("/login")
    async def login(body: LoginRequest) -> Response:
        """Verify password and mint a session cookie.

        Returns 401 on any failure (unknown user, wrong password,
        malformed hash). The route does NOT distinguish between
        "no such user" and "wrong password" — same status code,
        same response body, same timing path (the password
        verify runs against a sentinel hash when the user is
        missing so timing comparison is constant). This matches
        OWASP authentication cheat-sheet guidance.
        """
        username = body.username.strip().lower()
        password_hash = account_store.get_password_hash(username)
        # Always run a verify even on missing user — keeps the
        # response time roughly constant regardless of whether
        # the username exists. The dummy hash is the argon2
        # encoded form of "" which will never match a real
        # password, so the verify always raises here.
        if password_hash is None:
            with contextlib.suppress(InvalidPasswordError):
                verify_password(body.password, _DUMMY_HASH)
            _logger.info("auth/login: unknown user %s", _redact_for_log(username))
            return JSONResponse(status_code=401, content={"error": "invalid username or password"})

        try:
            verify_password(body.password, password_hash)
        except InvalidPasswordError:
            _logger.info("auth/login: bad password for %s", _redact_for_log(username))
            return JSONResponse(status_code=401, content={"error": "invalid username or password"})

        # Opportunistic rehash if argon2 parameters were upgraded.
        # Cheap and self-healing; failure is non-fatal (the user
        # still gets logged in with the old hash).
        if needs_rehash(password_hash):
            try:
                account_store.update_password(username, hash_password(body.password))
            except Exception as exc:  # noqa: BLE001 — never fail login on rehash
                _logger.warning(
                    "auth/login: rehash failed for %s: %s",
                    _redact_for_log(username),
                    exc,
                )

        now = int(time.time())
        account_store.mark_logged_in(username, now)
        # Apply the file-backed admin list (additive — never demotes).
        # Runs after the row is known to exist so the set_admin UPDATE
        # matches; before get_user so the response reflects the flag.
        promote_if_listed(admin_list, account_store, username)

        session_jwt = mint_session_cookie(
            user_id=username,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
        )

        user = account_store.get_user(username)
        # `user` cannot be None here — we just verified the password
        # against a row that exists. Defensive-coding the dereference
        # would only mask a SqlAlchemy bug, which we want to surface.
        assert user is not None
        body_payload = {
            "token": session_jwt,
            "expires_in": _session_max_age,
            "user": {"id": user.id, "is_admin": user.is_admin},
        }
        resp = JSONResponse(status_code=200, content=body_payload)
        _set_session_cookie(
            resp,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )
        _logger.info("auth/login: success for %s", _redact_for_log(username))
        return resp

    @router.post("/logout")
    async def logout() -> Response:
        """Clear the session cookie. Always 204 (no body)."""
        resp = Response(status_code=204)
        _clear_session_cookie(resp, cookie_name=_session_cookie, secure=_secure)
        return resp

    @router.get("/me")
    async def me(request: Request) -> Response:
        """Return the current user's identity, or 401."""
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})
        user = account_store.get_user(user_id)
        if user is None:
            # Cookie validated but user row gone — admin deleted them
            # mid-session. Surface as 401 so the frontend re-logs in.
            return JSONResponse(status_code=401, content={"error": "user no longer exists"})
        return JSONResponse(
            status_code=200,
            content={
                "id": user.id,
                "is_admin": user.is_admin,
                "created_at": user.created_at,
                "last_login_at": user.last_login_at,
            },
        )

    # ── Invite + register (admin-issued, self-serve) ──────────────

    @router.post("/invite")
    async def invite(request: Request, body: InviteRequest) -> Response:
        """Mint a single-use invite URL (admin only)."""
        admin_id = auth_provider.get_user_id(request)
        if admin_id is None or not account_store.is_admin(admin_id):
            return JSONResponse(status_code=403, content={"error": "admin only"})

        token_id = secrets.token_urlsafe(32)
        now = int(time.time())
        account_store.create_token(
            token_id,
            kind="invite",
            user_id=None,
            created_by=admin_id,
            created_at=now,
            expires_at=now + config.invite_ttl_seconds,
            invited_is_admin=body.is_admin,
        )
        # The full register URL is built off the configured base
        # URL so admins can share it directly. Including the token
        # in a query param (not a fragment) means the URL works in
        # the simplest case: paste into any client.
        register_url = f"{config.base_url}/register?invite={token_id}"
        _logger.info(
            "auth/invite: %s minted invite (admin=%s)",
            _redact_for_log(admin_id),
            body.is_admin,
        )
        return JSONResponse(
            status_code=200,
            content={
                "token": token_id,
                "register_url": register_url,
                "expires_at": now + config.invite_ttl_seconds,
                "is_admin": body.is_admin,
            },
        )

    @router.post("/register")
    async def register(body: RegisterRequest) -> Response:
        """Consume an invite, create the user, sign them in.

        Ordering: validate format → check name availability → THEN
        redeem the invite. Reversing this would burn the (single-use)
        invite on every name collision — and an attacker holding a
        valid invite URL could trivially DoS it with one POST using
        ``username: "admin"`` (always taken after bootstrap).
        Validation + cheap-read precede the token consumption.

        A narrow TOCTOU race remains (name becomes taken between
        the check and the INSERT). We catch the resulting ValueError
        from ``create_user_with_password`` and accept the invite
        is consumed in that case — the user was genuinely racing
        another registrant for the same token, which is fine.
        """
        err = _validate_username(body.username)
        if err is not None:
            return JSONResponse(status_code=400, content={"error": err})

        username = body.username.strip().lower()
        if account_store.get_user(username) is not None:
            # Don't burn the invite — the user can retry with a
            # different name on the same URL.
            return JSONResponse(status_code=409, content={"error": "username already taken"})

        now = int(time.time())
        token = account_store.redeem_token(body.invite, kind="invite", now_epoch_seconds=now)
        if token is None:
            # Don't distinguish missing / expired / already-redeemed
            # — keeps the route opaque to brute-force token guessing.
            return JSONResponse(status_code=400, content={"error": "invite invalid or expired"})

        try:
            user = account_store.create_user_with_password(
                username,
                hash_password(body.password),
                is_admin=token.invited_is_admin,
            )
        except ValueError:
            # Genuine race against another registrant using the same
            # invite — invite is now consumed, no recovery here.
            return JSONResponse(status_code=409, content={"error": "username already taken"})

        account_store.mark_logged_in(username, now)
        # Admin list applies to invite-registered users too (additive).
        # Re-fetch so the response reflects a promotion; the invite's
        # own ``invited_is_admin`` still applies independently.
        if promote_if_listed(admin_list, account_store, username):
            user = account_store.get_user(username) or user
        session_jwt = mint_session_cookie(
            user_id=username,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
        )
        resp = JSONResponse(
            status_code=200,
            content={
                "token": session_jwt,
                "expires_in": _session_max_age,
                "user": {"id": user.id, "is_admin": user.is_admin},
            },
        )
        _set_session_cookie(
            resp,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )
        _logger.info(
            "auth/register: %s created via invite (admin=%s)",
            _redact_for_log(username),
            token.invited_is_admin,
        )
        return resp

    @router.post("/setup")
    async def setup(body: SetupRequest) -> Response:
        """Create the FIRST admin account from the web UI (first-run claim).

        Unauthenticated by necessity — there's no one to authenticate
        AS before the first account exists. Hard-gated instead: it
        succeeds only while the instance has **zero** password-having
        accounts, and 409s forever after. So it can't escalate or add
        a second admin once setup is done. This is the Immich/Jellyfin
        first-run model: on a fresh remote deploy (Docker / Render /
        Railway), the first visitor claims the admin account by
        choosing a username + password in the browser — no container
        access, no log-digging.

        Local (loopback) deploys never reach this: bootstrap already
        auto-provisions the admin + signs the operator in. A deploy
        that pre-seeds ``OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`` also
        won't (the admin exists at boot, so this 409s).
        """
        # Gate: only while no account has been claimed yet. Checked
        # against the same "any password-having user" predicate the
        # bootstrap uses, so the two agree on what "set up" means.
        if any(u.has_password for u in account_store.list_users()):
            return JSONResponse(status_code=409, content={"error": "setup already completed"})

        username = body.username.strip().lower()
        now = int(time.time())
        try:
            user = account_store.create_user_with_password(
                username,
                hash_password(body.password),
                is_admin=True,
            )
        except ValueError:
            # Lost a race: another setup request claimed the instance
            # between our gate check and this insert. Either way, setup
            # is now done — surface the same terminal 409.
            return JSONResponse(status_code=409, content={"error": "setup already completed"})

        account_store.mark_logged_in(username, now)
        # Admin list applies (additive); the setup admin is already admin.
        if promote_if_listed(admin_list, account_store, username):
            user = account_store.get_user(username) or user
        # Loopback CLI handoff: the operator who spawned this local server via
        # `omnigent run` needs a CLI token too, not just the browser cookie.
        # bootstrap_admin only mints the token when an admin already exists at
        # boot; on a fresh first run the admin is claimed HERE, so mint it now
        # (loopback only) — otherwise the in-flight `run` 401s until the next
        # server boot. Gated to loopback, mirroring bootstrap, so a remote
        # deploy never writes a CLI token for the server's own host.
        from omnigent.server.accounts_bootstrap import (
            _is_loopback_base_url,
            _mint_loopback_cli_token,
        )

        if _is_loopback_base_url(config.base_url):
            _mint_loopback_cli_token(
                username,
                base_url=config.base_url,
                cookie_secret=config.cookie_secret,
                session_ttl_hours=config.session_ttl_hours,
            )
            # Single-user continuity: a loopback server flipping into accounts
            # mode is the same human's laptop. The pre-accounts chats are owned
            # by the reserved ``local`` user; hand them to the new admin so they
            # stay visible (sessions are listed by owner, with no admin-sees-all
            # bypass). Loopback-only, so a multi-user deploy never reassigns one
            # user's sessions to another.
            if permission_store is not None:
                permission_store.ensure_user(username, is_admin=True)
                permission_store.reassign_user_grants(RESERVED_USER_LOCAL, username)
        session_jwt = mint_session_cookie(
            user_id=username,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
        )
        resp = JSONResponse(
            status_code=200,
            content={
                "token": session_jwt,
                "expires_in": _session_max_age,
                "user": {"id": user.id, "is_admin": user.is_admin},
            },
        )
        _set_session_cookie(
            resp,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )
        _logger.info("auth/setup: first admin %s claimed the instance", _redact_for_log(username))
        return resp

    # ── Magic link (CLI → web UI handoff) ─────────────────────────

    @router.post("/magic")
    async def magic_mint(request: Request) -> Response:
        """Mint a short-TTL magic-login token for the current user.

        Called by the CLI after it has authenticated via password
        login or a stored cookie. The returned URL is opened in
        the user's browser; ``/auth/magic/redeem`` then signs the
        browser session in as the same user — zero password
        re-entry. TTL is intentionally short (default 10 min).
        """
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})

        token_id = secrets.token_urlsafe(32)
        now = int(time.time())
        account_store.create_token(
            token_id,
            kind="magic",
            user_id=user_id,
            created_by=None,
            created_at=now,
            expires_at=now + config.magic_ttl_seconds,
        )
        redeem_url = f"{config.base_url}/auth/magic/redeem?t={token_id}"
        return JSONResponse(
            status_code=200,
            content={
                "redeem_url": redeem_url,
                "expires_at": now + config.magic_ttl_seconds,
            },
        )

    @router.get("/magic/redeem")
    async def magic_redeem(request: Request) -> Response:
        """Consume a magic token and redirect to ``/``.

        Sets the session cookie as if the user had just typed
        their password. Always returns a redirect — on failure,
        redirects to ``/login?magic=expired`` so the login page
        can show a graceful error.
        """
        token_id = request.query_params.get("t", "").strip()
        if not token_id:
            return RedirectResponse(url="/login?magic=missing", status_code=302)

        now = int(time.time())
        token = account_store.redeem_token(token_id, kind="magic", now_epoch_seconds=now)
        if token is None or token.user_id is None:
            return RedirectResponse(url="/login?magic=expired", status_code=302)

        # Confirm the underlying user still exists (admin may have
        # deleted them after the token was minted).
        user = account_store.get_user(token.user_id)
        if user is None:
            return RedirectResponse(url="/login?magic=expired", status_code=302)

        account_store.mark_logged_in(token.user_id, now)
        # Admin list applies on magic-link sign-in too (additive).
        promote_if_listed(admin_list, account_store, token.user_id)
        session_jwt = mint_session_cookie(
            user_id=token.user_id,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider="accounts",
        )
        resp = RedirectResponse(url="/", status_code=302)
        _set_session_cookie(
            resp,
            session_jwt,
            cookie_name=_session_cookie,
            secure=_secure,
            max_age_seconds=_session_max_age,
        )
        _logger.info("auth/magic/redeem: signed in %s", _redact_for_log(token.user_id))
        return resp

    # ── Members admin (list, delete, reset password) ──────────────

    @router.get("/users")
    async def list_users(request: Request) -> Response:
        """List all users (admin only)."""
        admin_id = auth_provider.get_user_id(request)
        if admin_id is None or not account_store.is_admin(admin_id):
            return JSONResponse(status_code=403, content={"error": "admin only"})

        users = account_store.list_users()
        return JSONResponse(
            status_code=200,
            content={
                "users": [
                    {
                        "id": u.id,
                        "is_admin": u.is_admin,
                        "created_at": u.created_at,
                        "last_login_at": u.last_login_at,
                        "has_password": u.has_password,
                    }
                    for u in users
                ]
            },
        )

    @router.delete("/users/{user_id}")
    async def delete_user(request: Request, user_id: str) -> Response:
        """Remove a user (admin only).

        Refuses two cases:

        - **Self-delete** — the calling admin would lock themselves
          out of their own session (cookie still valid but row
          gone; every subsequent /v1/me 401s).
        - **Last admin** — the deploy must always have at least one
          admin or there's no recovery path. Previously this was a
          hardcoded "can't delete the user named 'admin'", but the
          bootstrap admin's name now defaults to the OS user
          (e.g. ``dhruv.gupta``), so the check generalizes to
          "would this leave zero admins". Same invariant, name-agnostic.
          Checked and applied atomically (see
          ``AccountStore.delete_user``) so two concurrent deletes of
          two different admins can't both slip past the check.
        """
        admin_id = auth_provider.get_user_id(request)
        if admin_id is None or not account_store.is_admin(admin_id):
            return JSONResponse(status_code=403, content={"error": "admin only"})
        if user_id == admin_id:
            return JSONResponse(status_code=400, content={"error": "cannot delete self"})

        result = account_store.delete_user(user_id)
        if result is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if result is False:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "cannot delete the last admin — promote another "
                    "user first or the deploy would have no recovery path"
                },
            )
        return Response(status_code=204)

    @router.post("/users/{user_id}/reset")
    async def reset_user_password(request: Request, user_id: str) -> Response:
        """Admin-initiated password reset.

        Mints a fresh random password, stores its hash, returns
        the plaintext exactly once to the calling admin who
        then DMs it to the user out-of-band. No SMTP needed.
        """
        admin_id = auth_provider.get_user_id(request)
        if admin_id is None or not account_store.is_admin(admin_id):
            return JSONResponse(status_code=403, content={"error": "admin only"})

        user = account_store.get_user(user_id)
        if user is None:
            return JSONResponse(status_code=404, content={"error": "not found"})

        new_password = secrets.token_urlsafe(16)
        account_store.update_password(user_id, hash_password(new_password))
        _logger.info(
            "auth/users/reset: %s reset by %s",
            _redact_for_log(user_id),
            _redact_for_log(admin_id),
        )
        return JSONResponse(
            status_code=200,
            content={"id": user_id, "new_password": new_password},
        )

    @router.post("/users/me/password")
    async def change_own_password(request: Request, body: ChangePasswordRequest) -> Response:
        """Self-serve password change (requires old password)."""
        user_id = auth_provider.get_user_id(request)
        if user_id is None:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})

        current_hash = account_store.get_password_hash(user_id)
        if current_hash is None:
            # User exists (cookie valid) but has no password — they
            # came in via header/oidc. Self-serve change is N/A.
            return JSONResponse(
                status_code=400,
                content={"error": "no password set for this account"},
            )

        try:
            verify_password(body.old_password, current_hash)
        except InvalidPasswordError:
            return JSONResponse(status_code=401, content={"error": "old password incorrect"})

        account_store.update_password(user_id, hash_password(body.new_password))
        _logger.info("auth/users/me/password: changed for %s", _redact_for_log(user_id))
        return Response(status_code=204)

    return router


# Pre-computed dummy hash for the timing-equalization path on
# /auth/login when the username is unknown. argon2 verify against
# this will always raise InvalidPasswordError — we only run it
# for the side effect of taking roughly the same wall time as a
# verify against a real stored hash, so an attacker can't use
# response latency to enumerate valid usernames.
_DUMMY_HASH = hash_password("__dummy_for_timing_equalization__")
