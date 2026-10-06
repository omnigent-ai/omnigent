"""OIDC authentication routes: login, callback, logout, CLI and native login.

Provides ``/auth/login``, ``/auth/callback``, ``/auth/logout``,
``/auth/cli-login``, ``/auth/cli-poll``, and ``/auth/native-token``
endpoints that implement the full OIDC authorization code flow with
PKCE. The ``cli-login`` / ``cli-poll`` pair supports the ``omnigent
login`` CLI command.

Native apps (the desktop shell) sign in through the system browser with
an RFC 8252 loopback redirect: ``/auth/login`` accepts a loopback
``native_redirect_uri`` plus a PKCE ``code_challenge``, the callback
redirects the browser to that loopback with a one-time code, and the
app exchanges the code and its verifier at ``/auth/native-token``. The
code only reaches the machine whose browser signed in, and is useless
without the verifier held by the app that started the flow.

See ``designs/OIDC_AUTH.md`` for the complete design.

These routes are only mounted when ``OMNIGENT_AUTH_PROVIDER=oidc``.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import jwt
from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.admin_list import AdminList, promote_if_listed
from omnigent.server.auth import (
    _RESERVED_USERS,
    UnifiedAuthProvider,
)
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.oidc import (
    _GITHUB_EMAILS_ENDPOINT,
    derive_code_challenge,
    generate_code_verifier,
    mint_session_cookie,
)
from omnigent.server.oidc_access import OidcAdmissionPolicy, resolve_allowed_domains_path
from omnigent.server.routes._oauth import NO_STORE_HEADERS, oauth_error
from omnigent.server.routes.device_auth import issue_login_grant
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# Short-lived cookie for PKCE state during the login flow.
_AUTH_STATE_COOKIE_SECURE = "__Host-ap_auth_state"
_AUTH_STATE_COOKIE_PLAIN = "ap_auth_state"
_AUTH_STATE_TTL_SECONDS = 300  # 5 minutes
_CLI_TICKET_TTL_SECONDS = 300  # 5 minutes
# A native sign-in code is exchanged by the app right after the browser
# hands it over, so it only needs to outlive one loopback round trip.
_NATIVE_CODE_TTL_SECONDS = 60
# RFC 8252 §7.3 loopback literals. ``localhost`` is excluded (§8.3): its
# resolution can be redirected away from this machine.
_NATIVE_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_NATIVE_PARAMS = ("native_redirect_uri", "native_state", "code_challenge", "code_challenge_method")
# RFC 7636: an S256 challenge is a 43-char base64url SHA-256 digest, and a
# verifier is 43-128 unreserved characters.
_S256_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_CODE_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_NATIVE_STATE_RE = re.compile(r"[A-Za-z0-9._~-]{1,256}")
# How long an OIDC invite URL stays redeemable. Matches the accounts
# provider's default invite window (72h) — long enough to share
# out-of-band, short enough to bound exposure of an unused link.
_OIDC_INVITE_TTL_SECONDS = 72 * 3600

if TYPE_CHECKING:
    from omnigent.server.oidc import OIDCConfig


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
    :param refresh_token: Login-issued refresh grant material, set at
        fulfillment when a grant store is wired. ``None`` while pending
        or when grants are unavailable. Handed to the CLI exactly once
        by the poll response.
    """

    created_at: float = field(default_factory=time.time)
    token: str | None = None
    user_id: str | None = None
    refresh_token: str | None = None


@dataclass
class _NativeCode:
    """A one-time code issued to a native app's loopback redirect.

    Created by ``/auth/callback`` for a native sign-in and consumed by
    ``POST /auth/native-token``.

    :param user_id: The authenticated user's email.
    :param code_challenge: The PKCE S256 challenge the app sent to
        ``/auth/login``; the exchange must present its verifier.
    :param redirect_uri: The loopback URI the code was delivered to; the
        exchange must name the same URI.
    :param created_at: Unix timestamp when the code was issued.
    """

    user_id: str
    code_challenge: str
    redirect_uri: str
    created_at: float = field(default_factory=time.time)


def create_auth_router(
    auth_provider: UnifiedAuthProvider,
    permission_store: PermissionStore | None,
    admin_list: AdminList,
    account_store: SqlAlchemyAccountStore | None = None,
    allowed_domains: frozenset[str] | None = None,
    device_grant_store: DeviceGrantStore | None = None,
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
    :param device_grant_store: When set, a CLI-ticket login also issues
        a refresh grant (see
        :func:`omnigent.server.routes.device_auth.issue_login_grant`)
        so hosts and CLIs can renew without a human re-running
        ``omnigent login``. ``None`` keeps the legacy
        session-JWT-only response.
    :returns: A FastAPI router with ``/login``, ``/callback``,
        ``/logout``, ``/cli-login``, ``/cli-poll``, ``/native-token``,
        ``/users`` (and ``/invite`` when invites are enabled).
    """
    router = APIRouter()
    config = auth_provider._oidc_config
    if config is None:
        raise ValueError("OIDC auth router requires an OIDC-configured auth provider")

    # Invites are opt-in AND require the token store. Both must hold.
    invite_store = account_store if config.allow_invites else None
    _invites_enabled = invite_store is not None

    # Admission policy: domain allowlist (env ∪ runtime-editable file)
    # with admin-list and (when enabled) invite bypasses. One place
    # decides who may sign in — see omnigent/server/oidc_access.py.
    admission = OidcAdmissionPolicy(
        env_allowed_domains=config.allowed_domains,
        domains_file_path=resolve_allowed_domains_path(),
        admin_list=admin_list,
        invited_lookup=invite_store,
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
    # One-time codes for native (loopback) sign-ins. Single-use, 60 s.
    _native_codes: dict[str, _NativeCode] = {}

    def _verified_state(request: Request, state: str | None) -> dict[str, object] | None:
        """Return the signed auth state when its cookie is valid and matches ``state``."""
        raw = request.cookies.get(_state_cookie)
        if not state or not raw:
            return None
        try:
            payload = jwt.decode(raw, config.cookie_secret, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            return None
        return payload if payload.get("state") == state else None

    def _native_failure(native: Mapping[str, str], error: str, description: str) -> Response:
        """Report a failed native sign-in to the app's loopback listener."""
        response = _native_redirect(native, {"error": error, "error_description": description})
        response.delete_cookie(
            key=_state_cookie, path="/", secure=_secure, httponly=True, samesite="lax"
        )
        return response

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
        # A bare "/" default (e.g. an invite link carrying no return_to) would
        # send the post-auth 302 to the origin root; keep it under the base path
        # so login stays within a subpath mount.
        base_path = getattr(request.app.state, "base_path", "")
        if base_path and return_to == "/":
            return_to = f"{base_path}/"
        # Optional CLI login ticket — threaded through the state
        # cookie so the callback can fulfill it.
        ticket = request.query_params.get("ticket")
        # Optional native (loopback) sign-in — also threaded through the
        # signed state, so the callback knows where to deliver the code.
        try:
            native = _parse_native_sign_in(request.query_params)
        except ValueError:
            return JSONResponse(
                status_code=400, content={"error": "Invalid native sign-in parameters"}
            )
        if native is not None and ticket:
            return JSONResponse(
                status_code=400,
                content={"error": "A native sign-in cannot also carry a CLI ticket"},
            )
        # Optional OIDC invite token — threaded through the signed state
        # cookie (not a bare query param) so it can't be tampered with
        # before the callback redeems it. Only meaningful when invites
        # are enabled; ignored otherwise.
        invite = request.query_params.get("invite") if _invites_enabled else None
        # Forced re-authentication for the device-consent anti-phishing
        # gate: reauth=1 tells the IdP to require the user to
        # re-authenticate rather than reusing an existing session
        # (OIDC Core 3.1.2.1 `prompt=login`, `max_age=0`).
        # Not applicable to GitHub OAuth, which has no prompt parameter.
        reauth = request.query_params.get("reauth") == "1" and config.provider_type != "github"

        # Store state + code_verifier in a short-lived signed cookie.
        state_payload: dict[str, object] = {
            "state": state,
            "code_verifier": code_verifier,
            "return_to": return_to,
            "exp": _auth_state_exp(),
        }
        if ticket:
            state_payload["ticket"] = ticket
        if native is not None:
            state_payload["native"] = native
        if invite:
            state_payload["invite"] = invite
        if reauth:
            # Record when we demanded fresh auth so /callback can verify the
            # id_token's auth_time proves the IdP actually re-authenticated
            # after this point (rather than silently reusing its session).
            state_payload["reauth_at"] = int(time.time())
        state_jwt = jwt.encode(state_payload, config.cookie_secret, algorithm="HS256")

        # Build the authorization URL.
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": config.scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if reauth:
            # Ask the IdP to force the user to re-enter credentials
            # rather than silently reusing an existing IdP session.
            params["prompt"] = "login"
            params["max_age"] = "0"
        endpoint = urlsplit(config.authorization_endpoint)
        query = [
            (name, value)
            for name, value in parse_qsl(endpoint.query, keep_blank_values=True)
            if name not in params
        ]
        auth_url = urlunsplit(endpoint._replace(query=urlencode(query + list(params.items()))))

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
            # An IdP error (e.g. the user declined) arrives without a code.
            # A native sign-in hears about it at its loopback listener.
            early_native = _native_from_state(_verified_state(request, state))
            if early_native is not None:
                return _native_failure(
                    early_native,
                    "access_denied",
                    "Sign-in was cancelled or declined at the identity provider.",
                )
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

        native = _native_from_state(state_payload)

        def fail(status_code: int, message: str, error: str = "access_denied") -> Response:
            # A native sign-in hears about failures at its loopback listener
            # instead of waiting out its timeout.
            if native is not None:
                return _native_failure(native, error, message)
            return JSONResponse(status_code=status_code, content={"error": message})

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
            "code_verifier": code_verifier,
        }
        if config.client_secret:
            token_data["client_secret"] = config.client_secret

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
                return fail(400, "Token exchange failed", "server_error")

            token_json = _json_object(_response_json(token_resp))
            if token_json is None:
                _logger.error("Token exchange returned a non-object JSON response")
                return fail(400, "Token exchange returned an invalid response", "server_error")

            # Extract user email.
            if config.provider_type == "github":
                access_token = token_json.get("access_token")
                email = await _resolve_github_email(
                    client,
                    access_token if isinstance(access_token, str) else "",
                )
            else:
                email = _resolve_oidc_email(token_json, config)

        if not email:
            return fail(400, "Could not determine user email from IdP", "server_error")

        # Forced re-auth verification (anti-phishing device-consent gate).
        # /login stamped reauth_at when it sent prompt=login + max_age=0.
        # A conformant IdP must then set the id_token's auth_time to the
        # actual (re-)authentication instant; if it silently reused its
        # session, auth_time predates our demand and we must refuse rather
        # than mint a fresh session that would pass the freshness gate.
        # GitHub has no id_token / auth_time, so reauth is never set for it.
        reauth_at = state_payload.get("reauth_at")
        if isinstance(reauth_at, int):
            auth_time = _resolve_oidc_auth_time(token_json, config)
            if auth_time is None:
                _logger.warning(
                    "Rejecting reauth login: IdP id_token has no auth_time claim, "
                    "so forced re-authentication (prompt=login) cannot be verified"
                )
                return fail(403, "IdP did not confirm re-authentication")
            if auth_time < reauth_at:
                _logger.warning(
                    "Rejecting reauth login: id_token auth_time %d predates the "
                    "re-authentication demand at %d (IdP reused its session)",
                    auth_time,
                    reauth_at,
                )
                return fail(403, "IdP did not re-authenticate the user")

        # Normalize email to lowercase.
        email = email.lower()

        # Redeem an OIDC invite (if one rode along in the signed state)
        # BEFORE the admission check, so the just-bound email passes the
        # domain gate via the invite bypass. Single-use: the token is
        # consumed here and stamped with this email on the existing
        # account_tokens row, which doubles as the durable pre-auth that
        # admits the email on subsequent logins. Reserved-name emails are
        # rejected below regardless, so binding one here is harmless.
        if invite_store is not None:
            invite_token = state_payload.get("invite")
            if invite_token:
                invite_store.redeem_oidc_invite(
                    str(invite_token), email, now_epoch_seconds=int(time.time())
                )

        # Admission control: domain allowlist (env ∪ file) plus the
        # admin-list / invite bypasses. An empty effective allowlist
        # means "no restriction" (admit any IdP user) — the OSS default.
        if not admission.is_admitted(email):
            domain = email.rsplit("@", 1)[-1] if "@" in email else ""
            return fail(403, f"Email domain {domain!r} is not permitted on this server")

        # Reject reserved user names.
        if email in _RESERVED_USERS:
            return fail(403, f"Reserved user name {email!r}")

        # Ensure user exists in the permission store, then apply the
        # file-backed admin list. Promotion is additive (never demotes)
        # and is OIDC's only path to admin — the IdP doesn't tell us
        # who is an operator. ensure_user must run first so the
        # set_admin UPDATE inside promote_if_listed matches a row.
        if permission_store is not None:
            permission_store.ensure_user(email)
            promote_if_listed(admin_list, permission_store, email)

        # A native sign-in gets a one-time code at its loopback listener;
        # the session is minted when the app exchanges it with its verifier.
        # The browser gets no session cookie: it never asked for one.
        if native is not None:
            _evict_expired_native_codes(_native_codes)
            native_code = secrets.token_urlsafe(32)
            _native_codes[native_code] = _NativeCode(
                user_id=email,
                code_challenge=native["code_challenge"],
                redirect_uri=native["redirect_uri"],
            )
            response = _native_redirect(native, {"code": native_code})
            response.delete_cookie(
                key=_state_cookie, path="/", secure=_secure, httponly=True, samesite="lax"
            )
            return response

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
            # A CLI login is a long-lived unattended credential holder
            # (hosts especially) — issue a refresh grant so it can renew
            # instead of dying at session-JWT expiry. Best-effort: a
            # grant-store failure must not break login itself.
            if device_grant_store is not None:
                try:
                    ticket.refresh_token = issue_login_grant(
                        device_grant_store,
                        user_id=email,
                        cookie_secret=config.cookie_secret,
                    )
                except Exception:
                    _logger.exception("cli-login: refresh grant issuance failed")
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
                "<p>You can close this tab and return to where you started signing in.</p>"
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

    if invite_store is not None:

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
            invite_store.create_token(
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
    async def logout(request: Request) -> Response:
        """Clear the session cookie and redirect.

        If ``OMNIGENT_OIDC_LOGOUT_REDIRECT_URI`` is configured,
        redirects to the IdP's end-session endpoint. Otherwise,
        redirects to the app root, kept under the deployment base
        path so sign-out does not escape a subpath mount.

        :returns: 302 redirect with the session cookie cleared.
        """
        base_path = getattr(request.app.state, "base_path", "")
        redirect_url = config.logout_redirect_uri or f"{base_path}/"
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
        refresh_token = ticket.refresh_token
        del _cli_tickets[ticket_id]
        content: dict[str, object] = {
            "token": token,
            "user_id": user_id,
            "expires_in": config.session_ttl_hours * 3600,
        }
        # Only present when a grant store is wired — old CLIs ignore the
        # extra key, new CLIs against old servers see it absent.
        if refresh_token is not None:
            content["refresh_token"] = refresh_token
        return JSONResponse(status_code=200, content=content)

    # ── Native (loopback) sign-in ───────────────────────────────────────────

    @router.post("/native-token")
    async def native_token(request: Request) -> Response:
        """Exchange a native sign-in code for a session token.

        The form carries ``code`` (delivered to the app's loopback
        listener by ``/auth/callback``), the PKCE ``code_verifier`` whose
        S256 digest the app sent to ``/auth/login``, and the same
        ``redirect_uri``. A code is consumed by its first exchange
        attempt, whatever the outcome.

        :param request: The incoming form-encoded POST.
        :returns: 200 ``{"token", "user_id", "expires_in"}`` plus
            ``refresh_token`` when a grant store is wired, or a 400
            RFC 6749 error (``invalid_request`` / ``invalid_grant``).
        """
        form = await request.form()
        code = form.get("code")
        verifier = form.get("code_verifier")
        redirect_uri = form.get("redirect_uri")
        if not isinstance(code, str) or not code:
            return oauth_error("invalid_request")
        _evict_expired_native_codes(_native_codes)
        # Consumed before anything else is checked, so even a malformed
        # attempt burns the code.
        pending = _native_codes.pop(code, None)
        if not isinstance(verifier, str) or not isinstance(redirect_uri, str):
            return oauth_error("invalid_request")
        if (
            pending is None
            or time.time() - pending.created_at > _NATIVE_CODE_TTL_SECONDS
            or redirect_uri != pending.redirect_uri
            or not _CODE_VERIFIER_RE.fullmatch(verifier)
            or not hmac.compare_digest(derive_code_challenge(verifier), pending.code_challenge)
        ):
            return oauth_error("invalid_grant")

        content: dict[str, object] = {
            "token": mint_session_cookie(
                user_id=pending.user_id,
                cookie_secret=config.cookie_secret,
                ttl_hours=config.session_ttl_hours,
                provider=config.provider_type,
            ),
            "user_id": pending.user_id,
            "expires_in": config.session_ttl_hours * 3600,
        }
        # The desktop renews from this grant instead of reopening the
        # browser when the session expires. Best-effort, as for CLI logins.
        if device_grant_store is not None:
            try:
                content["refresh_token"] = issue_login_grant(
                    device_grant_store,
                    user_id=pending.user_id,
                    cookie_secret=config.cookie_secret,
                )
            except Exception:
                _logger.exception("native sign-in: refresh grant issuance failed")
        return JSONResponse(status_code=200, content=content, headers=NO_STORE_HEADERS)

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


def _evict_expired_native_codes(codes: dict[str, _NativeCode]) -> None:
    """Remove native sign-in codes past their TTL.

    :param codes: The mutable code dict to prune.
    """
    now = time.time()
    expired = [k for k, v in codes.items() if now - v.created_at > _NATIVE_CODE_TTL_SECONDS]
    for k in expired:
        del codes[k]


def _is_loopback_redirect_uri(raw: str) -> bool:
    """Whether ``raw`` is an RFC 8252 loopback redirect a native app can listen on.

    Only ``http://127.0.0.1:<port>/...`` and ``http://[::1]:<port>/...``
    qualify: an explicit port, no userinfo, query, or fragment. Anything
    else could deliver the sign-in code off this machine.

    :param raw: The ``native_redirect_uri`` query parameter, e.g.
        ``"http://127.0.0.1:53682/callback"``.
    :returns: True when the URI is an acceptable loopback redirect.
    """
    if not raw or "?" in raw or "#" in raw or "\\" in raw or any(c.isspace() for c in raw):
        return False
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname in _NATIVE_LOOPBACK_HOSTS
        and port is not None
        and port > 0
        and parts.username is None
        and parts.password is None
    )


def _parse_native_sign_in(params: Mapping[str, str]) -> dict[str, str] | None:
    """Read the native sign-in parameters of a ``/auth/login`` request.

    :param params: The request's query parameters.
    :returns: ``{"redirect_uri", "state", "code_challenge"}``, or ``None``
        when the request carries no native parameter.
    :raises ValueError: When any native parameter is present but the set
        is incomplete or invalid. Only S256 challenges are accepted.
    """
    if not any(name in params for name in _NATIVE_PARAMS):
        return None
    redirect_uri = params.get("native_redirect_uri", "")
    native_state = params.get("native_state", "")
    challenge = params.get("code_challenge", "")
    if (
        params.get("code_challenge_method") != "S256"
        or not _is_loopback_redirect_uri(redirect_uri)
        or not _NATIVE_STATE_RE.fullmatch(native_state)
        or not _S256_CHALLENGE_RE.fullmatch(challenge)
    ):
        raise ValueError("invalid native sign-in parameters")
    return {"redirect_uri": redirect_uri, "state": native_state, "code_challenge": challenge}


def _native_from_state(state_payload: Mapping[str, object] | None) -> dict[str, str] | None:
    """Return the native sign-in recorded in a verified auth state, if any.

    :param state_payload: The decoded, signature-checked state cookie.
    :returns: The dict :func:`_parse_native_sign_in` produced at login, or
        ``None`` for a browser or CLI-ticket sign-in.
    """
    native = state_payload.get("native") if state_payload else None
    if not isinstance(native, dict):
        return None
    fields = ("redirect_uri", "state", "code_challenge")
    if not all(isinstance(native.get(name), str) for name in fields):
        return None
    return {name: native[name] for name in fields}


def _native_redirect(native: Mapping[str, str], params: dict[str, str]) -> RedirectResponse:
    """Redirect the browser to a native app's loopback listener.

    :param native: The native sign-in from :func:`_native_from_state`.
    :param params: Query parameters to deliver, e.g. ``{"code": ...}``;
        the app's ``state`` is appended.
    :returns: A 302 to the loopback redirect URI.
    """
    query = urlencode({**params, "state": native["state"]})
    return RedirectResponse(url=f"{native['redirect_uri']}?{query}", status_code=302)


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
        payload = _response_json(emails_resp)
        if not isinstance(payload, list):
            return None
        for raw_entry in payload:
            entry = _json_object(raw_entry)
            if entry is None:
                continue
            if entry.get("primary") and entry.get("verified"):
                email = entry.get("email")
                if isinstance(email, str) and email:
                    return email

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


def _validate_id_token(
    token_json: dict[str, object],
    config: OIDCConfig,
) -> dict[str, object] | None:
    """Validate the OIDC ``id_token`` and return its decoded claims.

    Checks the JWT signature against the IdP's JWKS and verifies the
    ``iss`` and ``aud`` claims. A valid signature proves IdP provenance
    only — callers must still gate on individual claims (email
    verification, ``auth_time`` freshness, …).

    :param token_json: Token endpoint response JSON with ``id_token``.
    :param config: OIDC config supplying JWKS URI, issuer, audience.
    :returns: Decoded claims, or ``None`` if the token is
        missing/malformed or fails validation.
    """
    id_token = token_json.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        return None
    if config.jwks_uri is None:
        _logger.warning("Rejecting id_token: OIDC configuration has no JWKS URI")
        return None

    try:
        jwks_client = jwt.PyJWKClient(config.jwks_uri)
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
        return jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "ES512"],
            audience=config.client_id,
            issuer=config.issuer,
        )
    except jwt.InvalidTokenError as exc:
        _logger.warning("id_token validation failed: %s", exc)
        return None


def _resolve_oidc_auth_time(
    token_json: dict[str, object],
    config: OIDCConfig,
) -> int | None:
    """Return the id_token's ``auth_time`` (last authentication instant).

    ``auth_time`` is the epoch second at which the IdP actually
    authenticated the end user. It is REQUIRED in the id_token when the
    request carried ``max_age`` (OIDC Core §3.1.3.7), which is exactly
    the forced-re-auth case. Used to verify the IdP honored
    ``prompt=login``/``max_age=0`` rather than silently reusing its
    session.

    :param token_json: Token endpoint response JSON with ``id_token``.
    :param config: OIDC config for signature/claim validation.
    :returns: ``auth_time`` as an int, or ``None`` when the token is
        invalid or the claim is absent/non-numeric.
    """
    claims = _validate_id_token(token_json, config)
    if claims is None:
        return None
    auth_time = claims.get("auth_time")
    if isinstance(auth_time, bool):
        return None
    if isinstance(auth_time, int):
        return auth_time
    if isinstance(auth_time, float):
        return int(auth_time)
    return None


def _resolve_oidc_email(
    token_json: dict[str, object],
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
    claims = _validate_id_token(token_json, config)
    if claims is None:
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


def _json_object(value: object) -> dict[str, object] | None:
    """Return a string-keyed JSON object, or ``None`` for other shapes."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast("dict[str, object]", value)


def _response_json(response: httpx.Response) -> object | None:
    """Decode a JSON response, returning ``None`` when decoding fails."""
    try:
        value: object = response.json()
    except ValueError:
        return None
    return value
