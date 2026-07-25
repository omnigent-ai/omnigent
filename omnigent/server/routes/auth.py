"""OIDC authentication routes: login, callback, logout, CLI login.

Provides ``/auth/login``, ``/auth/callback``, ``/auth/logout``,
``/auth/cli-login``, and ``/auth/cli-poll`` endpoints that implement
the full OIDC authorization code flow with PKCE. The ``cli-login``
/ ``cli-poll`` pair supports the ``omnigent login`` CLI command.

See ``designs/OIDC_AUTH.md`` for the complete design.

These routes are only mounted when ``OMNIGENT_AUTH_PROVIDER=oidc``.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Query, Request
from starlette.responses import RedirectResponse, Response

from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.admin_list import AdminList, promote_if_listed
from omnigent.server.auth import (
    _RESERVED_USERS,
    UnifiedAuthProvider,
)
from omnigent.server.oidc import (
    _GITHUB_EMAILS_ENDPOINT,
    derive_code_challenge,
    generate_code_verifier,
    mint_session_cookie,
)
from omnigent.server.oidc_access import OidcAdmissionPolicy, resolve_allowed_domains_path
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# Short-lived cookie for PKCE state during the login flow.
_AUTH_STATE_COOKIE_SECURE = "__Host-ap_auth_state"
_AUTH_STATE_COOKIE_PLAIN = "ap_auth_state"
_AUTH_STATE_TTL_SECONDS = 300  # 5 minutes
_CLI_TICKET_TTL_SECONDS = 300  # 5 minutes
# How long an OIDC invite URL stays redeemable. Matches the accounts
# provider's default invite window (72h) — long enough to share
# out-of-band, short enough to bound exposure of an unused link.
_OIDC_INVITE_TTL_SECONDS = 72 * 3600


@dataclass
class _CliTicket:
    """A pending CLI login ticket.

    Created by ``POST /auth/cli-login``, fulfilled by the browser
    callback, polled by ``GET /auth/cli-poll``.

    :param created_at: Unix timestamp when the ticket was created.
    :param token: The session JWT, set when the browser callback
        fulfills the ticket. ``None`` while pending.
    :param user_id: The authenticated user's email, set when
        fulfilled. ``None`` while pending.
    """

    created_at: float = field(default_factory=time.time)
    token: str | None = None
    user_id: str | None = None


def create_auth_router(
    auth_provider: UnifiedAuthProvider,
    permission_store: PermissionStore | None,
    admin_list: AdminList,
    account_store: SqlAlchemyAccountStore | None = None,
    allowed_domains: frozenset[str] | None = None,
) -> APIRouter:
    """Create an :class:`APIRouter` with OIDC login/callback/logout routes.

    :param auth_provider: The unified auth provider (must have
        ``_oidc_config`` set).
    :param permission_store: Permission store for user upsert on
        first login. ``None`` if permissions are disabled.
    :param admin_list: File-backed admin roster. Consulted on each
        callback to promote a listed email to admin (additive — see
        :mod:`omnigent.server.admin_list`). OIDC's only admin signal.
    :param account_store: Invite-token persistence, required only when
        ``OMNIGENT_OIDC_ALLOW_INVITES`` is on. ``None`` disables the
        invite routes entirely. Reuses the accounts provider's existing
        ``account_tokens`` table — the single-use invite token is
        stamped with the redeeming email and doubles as the durable
        pre-authorization (no OIDC-specific table).
    :param allowed_domains: Domains from the server config's
        ``allowed_domains:`` key, union'd with
        ``OMNIGENT_OIDC_ALLOWED_DOMAINS`` and the runtime-editable file
        in the admission policy.
    :returns: A FastAPI router with ``/login``, ``/callback``,
        ``/logout`` (and ``/invite`` when invites are enabled).
    """
    router = APIRouter()
    config = auth_provider._oidc_config

    # Invites are opt-in AND require the token store. Both must hold.
    _invites_enabled = config.allow_invites and account_store is not None

    # Admission policy: domain allowlist (env ∪ runtime-editable file)
    # with admin-list and (when enabled) invite bypasses. One place
    # decides who may sign in — see omnigent/server/oidc_access.py.
    admission = OidcAdmissionPolicy(
        env_allowed_domains=config.allowed_domains,
        domains_file_path=resolve_allowed_domains_path(),
        admin_list=admin_list,
        invited_lookup=account_store if _invites_enabled else None,
        config_allowed_domains=allowed_domains,
    )

    # Cookie names and secure flag depend on HTTP vs HTTPS (derived
    # from redirect_uri). The __Host- prefix requires HTTPS — using
    # it on http://localhost causes browsers to silently drop the
    # cookie, resulting in an infinite login redirect.
    _secure = config.secure_cookies
    _session_cookie = config.session_cookie_name
    _state_cookie = _AUTH_STATE_COOKIE_SECURE if _secure else _AUTH_STATE_COOKIE_PLAIN

    # In-memory store for CLI login tickets. Tickets are short-lived
    # (5 min) and single-use. Keyed by ticket ID.
    _cli_tickets: dict[str, _CliTicket] = {}

    @router.get("/login")
    async def login(request: Request) -> Response:
        """Redirect to the IdP's authorization endpoint.

        Generates PKCE ``code_verifier`` / ``code_challenge`` and a
        ``state`` parameter. Stores them in a short-lived signed
        cookie so the callback can verify the response.

        :param request: The incoming FastAPI request.
        :returns: 302 redirect to the IdP with PKCE and state
            params.
        """
        state = secrets.token_urlsafe(32)
        code_verifier = generate_code_verifier()
        code_challenge = derive_code_challenge(code_verifier)

        # Sanitize at ingest so only a safe same-origin path is ever
        # signed into the state cookie — prevents an open redirect on
        # the post-auth 302 in /callback.
        return_to = _sanitize_return_to(request.query_params.get("return_to"))
        # Optional CLI login ticket — threaded through the state
        # cookie so the callback can fulfill it.
        ticket = request.query_params.get("ticket")
        # Optional OIDC invite token — threaded through the signed state
        # cookie (not a bare query param) so it can't be tampered with
        # before the callback redeems it. Only meaningful when invites
        # are enabled; ignored otherwise.
        invite = request.query_params.get("invite") if _invites_enabled else None

        # Store state + code_verifier in a short-lived signed cookie.
        state_payload: dict[str, str | int] = {
            "state": state,
            "code_verifier": code_verifier,
            "return_to": return_to,
            "exp": _auth_state_exp(),
        }
        if ticket:
            state_payload["ticket"] = ticket
        if invite:
            state_payload["invite"] = invite
        state_jwt = jwt.encode(state_payload, config.cookie_secret, algorithm="HS256")

        # Build the authorization URL.
        params = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": config.scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url = config.authorization_endpoint + "?" + urlencode(params)

        response = RedirectResponse(url=auth_url, status_code=302)
        response.set_cookie(
            key=_state_cookie,
            value=state_jwt,
            max_age=_AUTH_STATE_TTL_SECONDS,
            httponly=True,
            secure=config.secure_cookies,
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/callback")
    async def callback(request: Request) -> Response:
        """Handle the IdP callback after user authentication.

        Validates the ``state`` parameter, exchanges the
        authorization code for tokens, extracts the user's email,
        mints a session cookie, and redirects to the app.

        :param request: The incoming FastAPI request containing
            ``code`` and ``state`` query parameters plus the
            ``__Host-ap_auth_state`` cookie.
        :returns: 302 redirect to the app with session cookie set,
            or 400/403 on validation failure.
        """
        from fastapi.responses import JSONResponse

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing code or state parameter"},
            )

        # Verify state from the cookie.
        state_cookie = request.cookies.get(_state_cookie)
        if not state_cookie:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing auth state cookie"},
            )

        try:
            state_payload = jwt.decode(state_cookie, config.cookie_secret, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid or expired auth state"},
            )

        if state != state_payload.get("state"):
            return JSONResponse(
                status_code=400,
                content={"error": "State mismatch (possible CSRF)"},
            )

        code_verifier = state_payload.get("code_verifier", "")
        # Re-sanitize on the way out: /login sanitizes at ingest, but a
        # cookie minted before this fix (or by a tampering attempt that
        # somehow forged a valid signature) must not yield an open
        # redirect at the 302 below.
        return_to = _sanitize_return_to(state_payload.get("return_to"))

        # Exchange authorization code for tokens.
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code_verifier": code_verifier,
        }

        async with httpx.AsyncClient() as client:
            # GitHub requires Accept: application/json to get JSON
            # response from the token endpoint.
            headers = {"Accept": "application/json"} if config.provider_type == "github" else {}
            token_resp = await client.post(
                config.token_endpoint,
                data=token_data,
                headers=headers,
                timeout=10.0,
            )

            if token_resp.status_code != 200:
                _logger.error(
                    "Token exchange failed: %d %s",
                    token_resp.status_code,
                    token_resp.text,
                )
                return JSONResponse(
                    status_code=400,
                    content={"error": "Token exchange failed"},
                )

            token_json = token_resp.json()

            # Extract user email.
            if config.provider_type == "github":
                email = await _resolve_github_email(client, token_json.get("access_token", ""))
            else:
                email = _resolve_oidc_email(token_json, config)

        if not email:
            return JSONResponse(
                status_code=400,
                content={"error": "Could not determine user email from IdP"},
            )

        # Normalize email to lowercase.
        email = email.lower()

        # Redeem an OIDC invite (if one rode along in the signed state)
        # BEFORE the admission check, so the just-bound email passes the
        # domain gate via the invite bypass. Single-use: the token is
        # consumed here and stamped with this email on the existing
        # account_tokens row, which doubles as the durable pre-auth that
        # admits the email on subsequent logins. Reserved-name emails are
        # rejected below regardless, so binding one here is harmless.
        if _invites_enabled:
            invite_token = state_payload.get("invite")
            if invite_token:
                account_store.redeem_oidc_invite(
                    str(invite_token), email, now_epoch_seconds=int(time.time())
                )

        # Admission control: domain allowlist (env ∪ file) plus the
        # admin-list / invite bypasses. An empty effective allowlist
        # means "no restriction" (admit any IdP user) — the OSS default.
        if not admission.is_admitted(email):
            domain = email.rsplit("@", 1)[-1] if "@" in email else ""
            return JSONResponse(
                status_code=403,
                content={"error": f"Email domain {domain!r} is not permitted on this server"},
            )

        # Reject reserved user names.
        if email in _RESERVED_USERS:
            return JSONResponse(
                status_code=403,
                content={"error": f"Reserved user name {email!r}"},
            )

        # Ensure user exists in the permission store, then apply the
        # file-backed admin list. Promotion is additive (never demotes)
        # and is OIDC's only path to admin — the IdP doesn't tell us
        # who is an operator. ensure_user must run first so the
        # set_admin UPDATE inside promote_if_listed matches a row.
        if permission_store is not None:
            permission_store.ensure_user(email)
            promote_if_listed(admin_list, permission_store, email)

        # Mint session cookie.
        session_jwt = mint_session_cookie(
            user_id=email,
            cookie_secret=config.cookie_secret,
            ttl_hours=config.session_ttl_hours,
            provider=config.provider_type,
        )

        # Check if this callback fulfills a CLI login ticket.
        ticket_id = state_payload.get("ticket")
        if ticket_id and ticket_id in _cli_tickets:
            ticket = _cli_tickets[ticket_id]
            ticket.token = session_jwt
            ticket.user_id = email
            # Return a simple HTML page — the CLI is polling
            # /auth/cli-poll and will pick up the token.
            import html as _html

            from starlette.responses import HTMLResponse

            safe_email = _html.escape(email)
            html = (
                "<html><body style='font-family:system-ui;text-align:center;"
                "padding:60px'>"
                "<h2>Login successful</h2>"
                f"<p>Authenticated as <strong>{safe_email}</strong>.</p>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            )
            resp = HTMLResponse(content=html)
            # Still set the session cookie (useful if they also open
            # the web UI in the same browser).
            resp.set_cookie(
                key=_session_cookie,
                value=session_jwt,
                max_age=config.session_ttl_hours * 3600,
                httponly=True,
                secure=_secure,
                samesite="lax",
                path="/",
            )
            resp.delete_cookie(
                key=_state_cookie,
                path="/",
                secure=_secure,
                httponly=True,
                samesite="lax",
            )
            return resp

        # Normal browser login — redirect back to the app.
        response = RedirectResponse(url=return_to, status_code=302)
        response.set_cookie(
            key=_session_cookie,
            value=session_jwt,
            max_age=config.session_ttl_hours * 3600,
            httponly=True,
            secure=_secure,
            samesite="lax",
            path="/",
        )
        # Clear the auth state cookie.
        response.delete_cookie(
            key=_state_cookie,
            path="/",
            secure=_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    if _invites_enabled:

        @router.post("/invite")
        async def oidc_invite(request: Request) -> Response:
            """Mint a single-use OIDC invite URL (admin only).

            Pre-authorizes whoever redeems the link: when they complete
            the OIDC flow via ``/auth/login?invite=<token>``, the invite
            token is stamped with their IdP-returned email and they're
            admitted past the domain allowlist. Lets an admin onboard a
            single external collaborator without widening the domain
            allowlist. Admin is gated on the same ``is_admin`` flag the
            rest of the app uses (set by the admin-list promotion at
            login), with the admin list as a direct fallback.

            :param request: The incoming request (carries the admin's
                session cookie).
            :returns: 200 with ``token`` / ``invite_url`` / ``expires_at``,
                401 if unauthenticated, 403 if not an admin.
            """
            from fastapi.responses import JSONResponse

            caller = auth_provider.get_user_id(request)
            if caller is None:
                return JSONResponse(status_code=401, content={"error": "not authenticated"})
            is_admin = (
                permission_store is not None and permission_store.is_admin(caller)
            ) or admin_list.is_admin(caller)
            if not is_admin:
                return JSONResponse(status_code=403, content={"error": "admin only"})

            token_id = secrets.token_urlsafe(32)
            now = int(time.time())
            account_store.create_token(
                token_id,
                kind="invite",
                user_id=None,
                created_by=caller,
                created_at=now,
                expires_at=now + _OIDC_INVITE_TTL_SECONDS,
            )
            invite_url = f"{config.base_url}/auth/login?invite={token_id}"
            return JSONResponse(
                status_code=200,
                content={
                    "token": token_id,
                    "invite_url": invite_url,
                    "expires_at": now + _OIDC_INVITE_TTL_SECONDS,
                },
            )

    @router.get("/logout")
    async def logout() -> Response:
        """Clear the session cookie and redirect.

        If ``OMNIGENT_OIDC_LOGOUT_REDIRECT_URI`` is configured,
        redirects to the IdP's end-session endpoint. Otherwise,
        redirects to ``/``.

        :returns: 302 redirect with the session cookie cleared.
        """
        redirect_url = config.logout_redirect_uri or "/"
        response = RedirectResponse(url=redirect_url, status_code=302)
        response.delete_cookie(
            key=_session_cookie,
            path="/",
            secure=_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    # ── CLI login ticket endpoints ─────────────────────────────

    @router.post("/cli-login")
    async def cli_login() -> dict[str, str]:
        """Create a one-time CLI login ticket.

        The CLI calls this, then opens the returned ``login_url``
        in the user's browser. The browser completes the OIDC flow,
        the callback fulfills the ticket, and the CLI polls
        ``/auth/cli-poll`` to retrieve the session token.

        :returns: ``{"ticket": "<id>", "login_url": "/auth/login?ticket=<id>"}``.
        """
        # Evict expired tickets to prevent unbounded growth.
        _evict_expired_tickets(_cli_tickets)

        ticket_id = secrets.token_urlsafe(32)
        _cli_tickets[ticket_id] = _CliTicket()
        return {
            "ticket": ticket_id,
            "login_url": f"/auth/login?ticket={ticket_id}",
        }

    @router.get("/cli-poll")
    async def cli_poll(request: Request) -> Response:
        """Poll for CLI login ticket completion.

        Returns 202 while the ticket is pending, 200 with the
        session token once the browser flow completes, or 410 if
        the ticket has expired or doesn't exist.

        :param request: The incoming FastAPI request with
            ``ticket`` query parameter.
        :returns: 202 (pending), 200 (completed), or 410 (expired).
        """
        from fastapi.responses import JSONResponse

        ticket_id = request.query_params.get("ticket")
        if not ticket_id or ticket_id not in _cli_tickets:
            return JSONResponse(
                status_code=410,
                content={"error": "Ticket not found or expired"},
            )

        ticket = _cli_tickets[ticket_id]

        # Check expiry.
        if time.time() - ticket.created_at > _CLI_TICKET_TTL_SECONDS:
            del _cli_tickets[ticket_id]
            return JSONResponse(
                status_code=410,
                content={"error": "Ticket expired"},
            )

        # Still pending — browser hasn't completed the flow yet.
        if ticket.token is None:
            return JSONResponse(
                status_code=202,
                content={"status": "pending"},
            )

        # Fulfilled — return the token and clean up.
        token = ticket.token
        user_id = ticket.user_id
        del _cli_tickets[ticket_id]
        return JSONResponse(
            status_code=200,
            content={
                "token": token,
                "user_id": user_id,
                "expires_in": config.session_ttl_hours * 3600,
            },
        )

    # ── Admin: read-only user list ────────────────────────────────

    @router.get("/users")
    async def list_users(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> Response:
        """List all users (admin only).

        The OIDC analog of the accounts provider's ``GET /auth/users``
        — same response shape, so the SPA's Members surface renders
        identically. This is the read-only discovery half of the
        admin surface: OIDC identities are owned by the IdP, so there
        are no server-side password actions (invite/reset/delete) to
        offer here, and the SPA hides those controls in OIDC mode.

        Admin is gated on the same ``is_admin`` flag the rest of the
        app uses (set by the admin-list promotion at login), with the
        admin list as a direct fallback — matching the OIDC invite
        route above.

        :param request: The incoming request (carries the session cookie).
        :returns: 200 with ``{"users": [...]}``, 401 if unauthenticated,
            403 if not an admin, or 200 with an empty list if no
            permission store is wired.
        """
        from fastapi.responses import JSONResponse

        caller = auth_provider.get_user_id(request)
        if caller is None:
            return JSONResponse(status_code=401, content={"error": "not authenticated"})
        is_admin = (
            permission_store is not None and permission_store.is_admin(caller)
        ) or admin_list.is_admin(caller)
        if not is_admin:
            return JSONResponse(status_code=403, content={"error": "admin only"})

        users = permission_store.list_users(limit=limit) if permission_store is not None else []
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

    return router


# ── Private helpers ──────────────────────────────────────────────


def _evict_expired_tickets(tickets: dict[str, _CliTicket]) -> None:
    """Remove expired CLI login tickets from the in-memory store.

    Called before creating a new ticket to prevent unbounded growth
    from abandoned login attempts.

    :param tickets: The mutable ticket dict to prune.
    """
    now = time.time()
    expired = [k for k, v in tickets.items() if now - v.created_at > _CLI_TICKET_TTL_SECONDS]
    for k in expired:
        del tickets[k]


def _sanitize_return_to(raw: str | None) -> str:
    """Reduce a caller-supplied ``return_to`` to a safe same-origin path.

    The OIDC login flow accepts a ``return_to`` query param and, after
    authentication, issues a server-side 302 to it. Without validation
    that is an open redirect: ``/auth/login?return_to=https://evil.example``
    would land the user on an attacker page under the app's own domain
    (phishing / credential-harvest vector). Signing ``return_to`` into
    the state cookie protects its *integrity* across the IdP round-trip
    but does nothing for its *safety* — the value still originates with
    the caller. This is the server-side mirror of ``sanitizeReturnTo``
    in ``web/src/pages/LoginPage.tsx``; the accounts flow navigates
    client-side and is already guarded there, but the OIDC redirect
    happens in Python and bypasses that check.

    Only a relative path on the same origin is allowed: it must start
    with a single ``/`` and must not start with ``//`` (a
    protocol-relative URL like ``//evil.example`` that browsers treat as
    cross-origin). Anything else — absolute URLs, scheme-bearing values,
    or an empty/``None`` value — falls back to ``"/"``. Query strings
    and fragments on an otherwise-relative path are preserved, so deep
    links such as ``/sessions/abc?tab=x`` round-trip unchanged.

    :param raw: The caller-supplied ``return_to`` value, e.g.
        ``"/sessions/abc?tab=files"`` (kept) or
        ``"https://evil.example"`` (rejected). ``None`` when the param
        was absent.
    :returns: A safe same-origin path, or ``"/"`` if ``raw`` is missing
        or not a same-origin relative path.
    """
    if not raw:
        return "/"
    # Must be a relative path; reject absolute/scheme-bearing URLs.
    if not raw.startswith("/"):
        return "/"
    # Reject protocol-relative ("//host") and scheme-relative ("/\\host")
    # forms that browsers resolve to a different origin.
    if raw.startswith(("//", "/\\")):
        return "/"
    return raw


def _auth_state_exp() -> int:
    """Return the expiration timestamp for the auth state cookie.

    :returns: Unix timestamp 5 minutes from now.
    """
    import time

    return int(time.time()) + _AUTH_STATE_TTL_SECONDS


async def _resolve_github_email(
    client: httpx.AsyncClient,
    access_token: str,
) -> str | None:
    """Fetch the primary *verified* email from GitHub's user API.

    Only a ``primary`` and ``verified`` address from ``/user/emails`` is
    returned. GitHub's ``/user.email`` (the public *profile* email) is not
    guaranteed to be verified or owned by the caller, so it is never used
    as the sign-in identity — trusting it would let a user log in as an
    arbitrary address they merely typed into their profile, bypassing the
    domain allowlist and (if that address is admin-listed) escalating to
    admin. This mirrors the ``email_verified`` gate the OIDC ``id_token``
    path already enforces.

    :param client: An active ``httpx.AsyncClient``.
    :param access_token: GitHub OAuth access token.
    :returns: The user's primary verified email, or ``None`` if none is
        available (the caller rejects a ``None`` email with 400).
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    # Fetch email list — primary verified email is the identity.
    emails_resp = await client.get(
        _GITHUB_EMAILS_ENDPOINT,
        headers=headers,
        timeout=10.0,
    )
    if emails_resp.status_code == 200:
        for entry in emails_resp.json():
            if entry.get("primary") and entry.get("verified"):
                return entry.get("email")

    # No primary, verified address. Deliberately do NOT fall back to the
    # ``/user.email`` profile field: it is unverified and attacker-settable,
    # so returning it would let a caller assume an identity they do not own.
    # Fail closed — the caller turns a ``None`` email into a 400.
    return None


def _claim_is_verified_true(value: object) -> bool:
    """Whether an ``email_verified``-style claim asserts verification.

    OpenID Connect Core §5.1 types ``email_verified`` as a boolean,
    but notes implementations may emit it as the *string* ``"true"``
    — so accept both. Everything else (``False``, ``"false"``,
    ``None``, absent, or any other value) is treated as *not*
    verified.

    :param value: The raw claim value as decoded from the
        ``id_token``, e.g. ``True``, ``"true"``, ``False``, or
        ``None`` when the claim is absent.
    :returns: ``True`` only when the value is boolean ``True`` or the
        case-insensitive string ``"true"``.
    """
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


def _resolve_oidc_email(
    token_json: dict,
    config: OIDCConfig,
) -> str | None:
    """Extract the verified email from the OIDC ``id_token``.

    Validates the JWT signature against the IdP's JWKS, verifies
    ``iss`` and ``aud`` claims, and returns the ``email`` claim
    **only when the IdP marked it verified** via ``email_verified``.

    A valid signature proves the token came from the IdP; it does
    *not* prove the user controls the email address. Without the
    ``email_verified`` gate, an IdP that lets a user set an arbitrary
    (unverified) email would let that user sign in as anyone in an
    allowed domain. This mirrors the GitHub path, which
    requires ``verified`` on the primary email.

    ``config.skip_email_verification`` (from
    ``OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION``) waives the gate for
    IdPs that omit the claim for directory-managed users (e.g. Okta
    without custom API Access Management).

    ``config.email_claim`` (from ``OMNIGENT_OIDC_EMAIL_CLAIM``) names
    the claim that carries the email identity, for IdPs that omit
    ``email`` (Microsoft Entra ID commonly issues only
    ``preferred_username``). ``email_verified`` refers to the ``email``
    claim, so a custom claim always needs the verification opt-out too.

    :param token_json: The token endpoint response JSON containing
        ``id_token``.
    :param config: The OIDC configuration with JWKS URI and
        expected issuer/audience.
    :returns: The user's email from the ``id_token`` when present and
        marked verified; ``None`` if the token is missing/invalid, the
        email claim is absent or not a non-empty string, or
        ``email_verified`` is not truthy (and verification is not
        skipped via config).
    """
    id_token = token_json.get("id_token")
    if not id_token:
        return None

    try:
        jwks_client = jwt.PyJWKClient(config.jwks_uri)
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=config.client_id,
            issuer=config.issuer,
        )
    except jwt.InvalidTokenError as exc:
        _logger.warning("id_token validation failed: %s", exc)
        return None

    email = claims.get(config.email_claim)
    if not isinstance(email, str) or not email.strip():
        _logger.warning(
            "Rejecting id_token: %r claim is missing or not a non-empty string "
            "(claims present: %s). "
            "IdPs that use a different claim for the email identity "
            "can set OMNIGENT_OIDC_EMAIL_CLAIM.",
            config.email_claim,
            sorted(claims.keys()),
        )
        return None
    email = email.strip()

    # ``email_verified`` refers to the ``email`` claim (OIDC core), so
    # it vouches nothing about a custom identity claim — a token can
    # carry ``email_verified: true`` for a *different* address than the
    # one being minted. A custom claim therefore always requires the
    # explicit opt-out, regardless of ``email_verified``.
    if config.email_claim != "email":
        if config.skip_email_verification:
            _logger.info(
                "Accepting id_token %s %r; the claim has no verified "
                "marker (OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION is set)",
                config.email_claim,
                email,
            )
            return email
        _logger.warning(
            "Rejecting id_token: %s %r has no email_verified marker "
            "(email_verified refers to the email claim); set "
            "OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION to accept it",
            config.email_claim,
            email,
        )
        return None

    # Reject unless the IdP affirmatively verified the email. A signed
    # token only proves IdP provenance, not mailbox ownership.
    # Absent/false ``email_verified`` is a hard reject — unless the
    # operator opted out (OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION) for
    # IdPs like Okta that omit the claim for directory-managed users.
    if not _claim_is_verified_true(claims.get("email_verified")):
        if config.skip_email_verification:
            _logger.info(
                "Accepting id_token email %r without email_verified "
                "(OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION is set)",
                email,
            )
            return email
        _logger.warning(
            "Rejecting id_token: email %r present but email_verified is not true",
            email,
        )
        return None

    return email


# Forward ref for type annotation.
if False:  # TYPE_CHECKING
    from omnigent.server.oidc import OIDCConfig
