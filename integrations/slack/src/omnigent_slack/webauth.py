"""Enrollment web server for the Databricks U2M OAuth flow.

Runs inside the bot process and serves the OAuth **redirect callback** of a
custom Databricks OAuth app (authorization code + PKCE). Routes, behind the
bot's own Databricks App URL:

- ``GET /`` / ``GET /health`` — liveness for the platform.
- ``GET /auth/callback?code=<…>&state=<signed>`` — the OAuth redirect landing.
  Databricks sends the browser here after the user signs in. This route verifies
  the signed ``state`` (which binds the session to the Slack ``(team, user,
  email)`` that requested it and carries a single-use nonce), consumes the PKCE
  ``code_verifier`` the bot generated for that nonce, exchanges the code for a
  durable **access + refresh** token, checks that the OAuth-authenticated email
  matches the requesting Slack user, then shows a **consent page** naming both
  identities — storing **nothing** yet.
- ``POST /auth/callback`` — submitted by the consent page's Confirm button.
  Persists the tokens exchanged on the GET (stashed under a single-use confirm
  id) so the Socket-Mode bot can act as the user — and refresh without
  re-enrollment. Storing only on this explicit POST means a credential is never
  persisted without the user affirming the Omnigent↔Slack account linkage.

The authorization code is single-use, so it's exchanged once on the GET and the
resulting tokens held in a short-lived in-memory stash until the confirming POST.

The state signing/verification lives in :mod:`omnigent_slack.enrollment_state`;
the OAuth client in :mod:`omnigent_slack.databricks_oauth`. See
``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``.
"""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from omnigent_slack.config import Settings
from omnigent_slack.databricks_oauth import (
    DatabricksOAuthClient,
    DatabricksOAuthError,
    DatabricksTokens,
    derive_code_challenge,
    generate_code_verifier,
)
from omnigent_slack.enrollment_state import (
    EnrollmentState,
    StateError,
    emails_match,
    new_nonce,
    sign_state,
    verify_state,
)
from omnigent_slack.tokens import TokenStore

_logger = logging.getLogger(__name__)

# Fired after a user's token is stored, with (team_id, user_id, server_url) —
# same contract as AuthManager's hook, so the client pool drops any stale
# tokenless client and rebuilds with the fresh token.
EnrolledHook = Callable[[str, str, str], Awaitable[None]]

# How long a generated PKCE verifier is kept waiting for its callback. The user
# signs in within seconds; matches the state TTL so the two expire together.
_VERIFIER_TTL_SECONDS = 600

# How long exchanged-but-unconfirmed tokens wait for the user's Confirm click.
# Matches the setup modal's enrollment poll window (auth_manager, 600s) so a user
# who lingers on the consent page can still confirm while the modal is waiting —
# a shorter window would expire the confirm id mid-poll and dead-end the modal.
_PENDING_CONFIRM_TTL_SECONDS = 600


@dataclass(frozen=True, slots=True)
class _PendingConfirmation:
    """Tokens exchanged on the GET callback, awaiting the user's Confirm POST.

    The OAuth ``code`` is single-use, so it's exchanged once (on GET) and the
    resulting tokens held here until the user confirms the account linkage on the
    consent page — mirroring the old confirm-before-store flow. Bound to the Slack
    identity from the signed state so the POST stores under the right key.
    """

    enrollment: EnrollmentState
    tokens: DatabricksTokens
    expires_at: float


class WebAuthServer:
    """aiohttp app serving the Databricks OAuth redirect callback.

    :param settings: Loaded bot settings (databricks mode + OAuth config).
    :param token_store: Shared token backend — the same instance the bot's
        client pool reads, so a token stored here is immediately usable.
    :param on_enrolled: Optional hook fired after a token is stored, so the
        client pool can drop a stale tokenless client for the user.
    :param oauth_client: The workspace OAuth client. Defaults to one built from
        ``settings``; injectable for tests.
    """

    def __init__(
        self,
        settings: Settings,
        token_store: TokenStore,
        on_enrolled: EnrolledHook | None = None,
        oauth_client: DatabricksOAuthClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_store
        self._on_enrolled = on_enrolled
        self._oauth = oauth_client or _build_oauth_client(settings)
        self._runner: web.AppRunner | None = None
        # Single-use PKCE verifiers awaiting their OAuth callback, keyed by the
        # nonce carried in the signed state. In-memory, so this REQUIRES a single
        # app replica: the instance that minted the link (enrollment_url) must be
        # the one that handles the callback, else the verifier isn't found and
        # enrollment fails closed. nonce -> (code_verifier, expires_at).
        self._pending: dict[str, tuple[str, float]] = {}
        # Tokens exchanged on GET, awaiting the user's Confirm POST. Keyed by a
        # single-use confirm id embedded in the consent page's form. Same
        # single-replica constraint as _pending.
        self._pending_confirm: dict[str, _PendingConfirmation] = {}

    def enrollment_url(
        self, team_id: str, user_id: str, email: str, team_name: str = ""
    ) -> str | None:
        """Build the Databricks authorize link to post into Slack, or ``None``.

        Generates a PKCE verifier/challenge and a single-use nonce, stashes the
        verifier under the nonce, signs the nonce + Slack identity into the OAuth
        ``state``, and returns the workspace ``/oidc/v1/authorize`` URL. ``email``
        is the Slack user's email (from ``users.info``); it is signed into the
        state and later matched against the OAuth-authenticated email, so the
        enrolled token can only be the requesting user's own. ``None`` when the
        redirect URI isn't configured (no ``OMNIGENT_SLACK_WEBAUTH_BASE_URL`` /
        ``DATABRICKS_APP_URL``), the state secret is missing, or the email is
        absent — so the caller surfaces a clear message instead of a broken (or
        unverifiable) link.
        """
        secret = self._settings.databricks_state_secret
        if not self._settings.databricks_redirect_uri or not secret or not email:
            return None
        verifier = generate_code_verifier()
        nonce = new_nonce()
        self._prune_pending()
        self._pending[nonce] = (verifier, time.time() + _VERIFIER_TTL_SECONDS)
        state = sign_state(team_id, user_id, email, secret, nonce=nonce, team_name=team_name)
        try:
            return self._oauth.authorize_url(
                state=state, code_challenge=derive_code_challenge(verifier)
            )
        except DatabricksOAuthError as exc:
            _logger.warning("Could not build authorize URL: %s", exc)
            self._pending.pop(nonce, None)
            return None

    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_health),
                web.get("/health", self._handle_health),
                # GET is the OAuth redirect: it exchanges the code and shows a
                # consent page naming the identities. POST — the consent page's
                # Confirm button — is the only thing that stores the token, so a
                # credential is never persisted without the user affirming the
                # Omnigent↔Slack account linkage.
                web.get("/auth/callback", self._handle_callback),
                web.post("/auth/callback", self._handle_confirm),
            ]
        )
        return app

    async def start(self) -> None:
        """Bind the web server on the configured port (non-blocking)."""
        app = self.build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        port = self._settings.databricks_webauth_port
        site = web.TCPSite(self._runner, host="0.0.0.0", port=port)
        await site.start()
        _logger.info("Databricks web-auth server listening on 0.0.0.0:%d", port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.Response(text="ok")

    def _prune_pending(self) -> None:
        """Drop verifiers whose callback never came, bounding memory growth."""
        now = time.time()
        expired = [n for n, (_v, exp) in self._pending.items() if exp <= now]
        for nonce in expired:
            del self._pending[nonce]

    def _consume_verifier(self, nonce: str) -> str | None:
        """Pop the PKCE verifier for a nonce, or ``None`` if missing/expired.

        Single-use: popping means a replayed redirect (same state) finds nothing
        and is refused, on top of the code itself being single-use at Databricks.
        """
        self._prune_pending()
        entry = self._pending.pop(nonce, None)
        if entry is None:
            return None
        verifier, expires_at = entry
        return verifier if expires_at > time.time() else None

    def _verify_state(self, request: web.Request) -> EnrollmentState:
        """Verify the signed OAuth ``state`` or raise an HTML error response."""
        secret = self._settings.databricks_state_secret or ""
        try:
            return verify_state(request.query.get("state", ""), secret)
        except StateError as exc:
            _logger.info("Enrollment state rejected: %s", exc)
            raise _error(400, str(exc)) from exc

    def _prune_pending_confirm(self) -> None:
        # Drop exchanged-but-unconfirmed tokens past their TTL. Only the in-memory
        # copy is dropped — the underlying grant is NOT revoked at Databricks (it
        # lives out its own expiry). Acceptable: it's the user's own credential,
        # blast radius nil, and revoking here would need an async call from this
        # sync path for no security gain.
        now = time.time()
        expired = [k for k, p in self._pending_confirm.items() if p.expires_at <= now]
        for confirm_id in expired:
            del self._pending_confirm[confirm_id]

    def _consume_confirmation(self, confirm_id: str) -> _PendingConfirmation | None:
        """Pop exchanged-but-unconfirmed tokens by confirm id (single-use)."""
        self._prune_pending_confirm()
        pending = self._pending_confirm.pop(confirm_id, None)
        if pending is None or pending.expires_at <= time.time():
            return None
        return pending

    async def _handle_callback(self, request: web.Request) -> web.Response:
        """GET: exchange the OAuth code, then show a consent page (persist nothing).

        Verifies the signed state, consumes the matching PKCE verifier, exchanges
        the authorization code (PKCE-bound, single-use) for an access + refresh
        pair, then — the confused-deputy guard — requires the OAuth-authenticated
        email to equal the email the link was issued for. On success it stashes
        the tokens under a single-use confirm id and renders a consent page naming
        the exact Omnigent + Slack identities; the token is persisted to the store
        only when the user clicks Confirm (the POST). The code is single-use, so
        it's exchanged here (not re-exchanged on the POST).

        NOTE: the exchange does mint a real, live grant at Databricks — held only
        in ``_pending_confirm`` (never the persistent store) pre-Confirm. If the
        user abandons the flow, prune just drops the in-memory copy and the grant
        lives out its own expiry at Databricks (we don't revoke it). That's
        acceptable: it's the user's OWN credential, so the blast radius is nil —
        but it means "not persisted" is not the same as "no credential exists".
        """
        # Databricks appends ?error=access_denied when the user cancels consent.
        oauth_error = request.query.get("error")
        if oauth_error:
            _logger.info("OAuth callback returned error=%s", oauth_error)
            raise _error(400, "Sign-in was cancelled or denied. Start again from Slack.")

        enrollment = self._verify_state(request)
        code = request.query.get("code", "")
        if not code:
            raise _error(400, "The sign-in response was missing its authorization code.")

        verifier = self._consume_verifier(enrollment.nonce)
        if verifier is None:
            # Replayed or expired link — the verifier is gone (single-use / TTL).
            raise _error(
                400,
                "This sign-in link was already used or has expired. Start again from Slack.",
            )

        try:
            tokens = await self._oauth.exchange_code(code=code, code_verifier=verifier)
        except DatabricksOAuthError as exc:
            _logger.warning(
                "Code exchange failed team=%s user=%s: %s",
                enrollment.team_id,
                enrollment.user_id,
                exc,
            )
            raise _error(502, "Databricks rejected the sign-in. Please try again.") from exc

        # CONFUSED-DEPUTY GUARD. The signed state proves which Slack user
        # requested the link; the OAuth-authenticated email proves who actually
        # signed in. They must be the same person — otherwise a link issued for
        # Slack user A, opened by victim V, would store V's token under A.
        if not tokens.email or not emails_match(tokens.email, enrollment.email):
            _logger.warning(
                "Enrollment identity mismatch team=%s user=%s "
                "state_email=%s oauth_email=%s — refusing to store token",
                enrollment.team_id,
                enrollment.user_id,
                enrollment.email,
                tokens.email,
            )
            raise _error(
                403,
                "You signed in as a different account than this link was issued "
                "for. Start again from Slack and sign in as yourself.",
            )

        # Exchange succeeded and the identity matches — but persist NOTHING to the
        # store yet. Stash the (now-live) tokens under a single-use confirm id in
        # memory and ask the user to confirm the account linkage on a page that
        # names both identities; the store write happens only on the Confirm POST.
        self._prune_pending_confirm()
        confirm_id = new_nonce()
        self._pending_confirm[confirm_id] = _PendingConfirmation(
            enrollment=enrollment,
            tokens=tokens,
            expires_at=time.time() + _PENDING_CONFIRM_TTL_SECONDS,
        )
        _logger.info(
            "Awaiting connection confirmation team=%s user=%s",
            enrollment.team_id,
            enrollment.user_id,
        )
        return _html_response(
            _consent_page(
                confirm_id=confirm_id,
                server_url=self._settings.server_url,
                idp_email=tokens.email,
                slack_email=enrollment.email,
                team_name=enrollment.team_name,
            )
        )

    async def _handle_confirm(self, request: web.Request) -> web.Response:
        """POST: the consent page's Confirm — persist the exchanged tokens.

        Looks up the single-use confirm id, then stores the tokens for the Slack
        identity the signed state bound them to. Storing only on this explicit
        POST means a credential is never persisted without the user affirming the
        Omnigent↔Slack linkage on a page that named both identities.
        """
        data = await request.post()
        confirm_id = str(data.get("confirm_id", ""))
        pending = self._consume_confirmation(confirm_id)
        if pending is None:
            raise _error(
                400,
                "This confirmation has expired or was already used. Start again from Slack.",
            )
        enrollment = pending.enrollment
        tokens = pending.tokens
        server_url = self._settings.server_url
        await self._tokens.put(
            enrollment.team_id,
            enrollment.user_id,
            server_url,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
        )
        # The setup modal advances by polling the store for this exact key, so log
        # it — a key mismatch here vs. the poll is the one thing that leaves the
        # browser on the success page while the modal never confirms.
        _logger.info(
            "Stored enrollment token team=%s user=%s server=%s refreshable=%s",
            enrollment.team_id,
            enrollment.user_id,
            server_url,
            bool(tokens.refresh_token),
        )
        if self._on_enrolled is not None:
            await self._on_enrolled(enrollment.team_id, enrollment.user_id, server_url)
        return _html_response(
            _success_page(
                server_url=server_url,
                idp_email=tokens.email,
                slack_email=enrollment.email,
                team_name=enrollment.team_name,
            )
        )


def _build_oauth_client(settings: Settings) -> DatabricksOAuthClient:
    """Construct the workspace OAuth client from settings.

    Tolerates missing config (returns a client that raises on use) so a bot in
    ``auto`` mode — which never touches this — can still import the module and
    build an inert server; databricks mode validates the config at startup.
    """
    return DatabricksOAuthClient(
        settings.databricks_workspace_host or "",
        client_id=settings.databricks_oauth_client_id or "",
        client_secret=settings.databricks_oauth_client_secret or "",
        scopes=settings.databricks_oauth_scopes_normalized,
        redirect_uri=settings.databricks_redirect_uri,
    )


# Response headers for every callback page. The consent page embeds both email
# addresses (PII) and the confirm_id, so keep it out of browser history/bfcache
# on shared machines (``no-store``); the anti-framing/sniff/referrer headers are
# cheap defense-in-depth for an interstitial that carries identity data.
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _html_response(body: str, *, status: int = 200) -> web.Response:
    return web.Response(
        text=body, status=status, content_type="text/html", headers=_SECURITY_HEADERS
    )


# Status → aiohttp exception for the error responses the callback can raise.
_HTTP_ERRORS: dict[int, type[web.HTTPException]] = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
    502: web.HTTPBadGateway,
}


def _error(status: int, reason: str) -> web.HTTPException:
    """Build a raisable HTML error response with a friendly page body."""
    return _HTTP_ERRORS[status](
        text=_error_page(reason), content_type="text/html", headers=_SECURITY_HEADERS
    )


def _identity_summary(
    *, verb: str, server_url: str, idp_email: str, slack_email: str, team_name: str
) -> str:
    """Escaped one-sentence description of the account linkage.

    ``verb`` is "about to connect" (consent) or "connected" (success). All
    interpolated values are HTML-escaped (they come from the OAuth id_token /
    Slack), so the result is trusted HTML the pages embed directly.
    """
    workspace = f" in Slack workspace <b>{html.escape(team_name)}</b>" if team_name else ""
    return (
        f"You are {verb} your Omnigent <b>{html.escape(server_url)}</b> account "
        f"<b>{html.escape(idp_email)}</b> with Slack user "
        f"<b>{html.escape(slack_email)}</b>{workspace}."
    )


def _consent_page(
    *, confirm_id: str, server_url: str, idp_email: str, slack_email: str, team_name: str
) -> str:
    # Shown after the code exchange but BEFORE anything is stored. Names the exact
    # Omnigent + Slack identities being linked and requires an explicit Confirm (a
    # POST carrying the single-use confirm id) before the token is saved — so a
    # credential is never persisted without the user affirming the linkage.
    summary = _identity_summary(
        verb="about to connect",
        server_url=server_url,
        idp_email=idp_email,
        slack_email=slack_email,
        team_name=team_name,
    )
    message = (
        f"{summary}<br><br>"
        "Only continue if <b>all of the above</b> are correct and this is you. If "
        "anything is unrecognized, do <b>NOT</b> confirm — doing so lets that Slack "
        "user act as you and use your Omnigent account. Close this tab instead."
        "<br><br>"
        '<form method="post" action="/auth/callback">'
        f'<input type="hidden" name="confirm_id" value="{html.escape(confirm_id)}">'
        '<button type="submit" style="font-size:1rem;padding:0.6rem 1.2rem;'
        'border:0;border-radius:6px;background:#1a1a1a;color:#fff;cursor:pointer">'
        "Confirm &amp; connect</button></form>"
    )
    return _page("Confirm your Omnigent connection", message)


def _success_page(*, server_url: str, idp_email: str, slack_email: str, team_name: str) -> str:
    workspace = f" in Slack workspace <b>{html.escape(team_name)}</b>" if team_name else ""
    summary = (
        f"You connected your Omnigent <b>{html.escape(server_url)}</b> account "
        f"<b>{html.escape(idp_email)}</b> with Slack user "
        f"<b>{html.escape(slack_email)}</b>{workspace}."
    )
    message = (
        f"{summary}<br><br>"
        "Close this tab and return to Slack — mention the bot to start. To undo "
        "this, run <code>/omnigent logout</code> in Slack."
    )
    return _page("You're connected", message)


def _error_page(reason: str) -> str:
    return _page("Sign-in didn't complete", html.escape(reason))


def _page(title: str, message: str) -> str:
    # Minimal self-contained page — no external assets (the app runs behind a
    # locked-down proxy). ``title`` is always a static literal; ``message`` is
    # trusted HTML the callers assemble (they html.escape any dynamic values
    # before embedding), so it is intentionally not re-escaped here.
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        "padding:0 1rem;line-height:1.5;color:#1a1a1a}h1{font-size:1.4rem}</style>"
        f"</head><body><h1>{title}</h1><p>{message}</p></body></html>"
    )
