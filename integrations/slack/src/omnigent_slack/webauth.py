"""Enrollment web server for the Databricks Apps web-auth flow.

Runs inside the bot process as a second Databricks App (user authorization
enabled). It serves these routes behind the Databricks Apps proxy:

- ``GET /`` / ``GET /health`` — liveness for the platform.
- ``GET /auth/callback?state=<signed>`` — the enrollment landing. The proxy has
  already authenticated the browser user and injected ``x-forwarded-access-token``
  + ``x-forwarded-email``. This route verifies the signed ``state`` (which binds
  the session to the Slack ``(team, user, email)`` that requested it) and that
  the browser's email matches, then shows a **consent page** naming the exact
  identities — but stores nothing.
- ``POST /auth/callback?state=<signed>`` — submitted by the consent page's
  Confirm button. Re-validates, then stores the forwarded token for that
  ``(team, user, server)`` so the Socket-Mode bot can act as the user. Storing
  only on an explicit POST means a credential is never persisted without the
  browser user confirming their own identities on a page.

The state signing/verification lives in :mod:`omnigent_slack.enrollment_state`;
this module is the aiohttp wiring and the HTML pages. There is no token
exchange — the forwarded token is passed straight through (Databricks OBO;
audience-scoping is unavailable for access-token subjects). See
``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from omnigent_slack.config import Settings
from omnigent_slack.enrollment_state import (
    FORWARDED_ACCESS_TOKEN_HEADER,
    FORWARDED_EMAIL_HEADER,
    EnrollmentState,
    StateError,
    emails_match,
    sign_state,
    verify_state,
)
from omnigent_slack.tokens import TokenStore

_logger = logging.getLogger(__name__)

# Fired after a user's token is stored, with (team_id, user_id, server_url) —
# same contract as AuthManager's hook, so the client pool drops any stale
# tokenless client and rebuilds with the fresh token.
EnrolledHook = Callable[[str, str, str], Awaitable[None]]


class WebAuthServer:
    """aiohttp app serving the Databricks enrollment landing page.

    :param settings: Loaded bot settings (databricks mode + web-auth config).
    :param token_store: Shared token backend — the same instance the bot's
        client pool reads, so a token stored here is immediately usable.
    :param on_enrolled: Optional hook fired after a token is stored, so the
        client pool can drop a stale tokenless client for the user.
    """

    def __init__(
        self,
        settings: Settings,
        token_store: TokenStore,
        on_enrolled: EnrolledHook | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_store
        self._on_enrolled = on_enrolled
        self._runner: web.AppRunner | None = None

    def enrollment_url(
        self, team_id: str, user_id: str, email: str, team_name: str = ""
    ) -> str | None:
        """Build the signed enrollment link to post into Slack, or ``None``.

        ``email`` is the Slack user's email (from ``users.info``); it is signed
        into the state and later matched against the browser's
        ``X-Forwarded-Email`` in the callback, so the enrolled token can only be
        the requesting user's own. ``team_name`` is display-only (shown on the
        success page). ``None`` when the public base URL isn't configured (no
        ``OMNIGENT_SLACK_WEBAUTH_BASE_URL`` / ``DATABRICKS_APP_URL``) or the
        email is missing, so the caller surfaces a clear message instead of a
        broken (or unverifiable) link.
        """
        base = self._settings.webauth_base_url
        secret = self._settings.databricks_state_secret
        if not base or not secret or not email:
            return None
        state = sign_state(team_id, user_id, email, secret, team_name=team_name)
        return f"{base}/auth/callback?state={state}"

    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_health),
                web.get("/health", self._handle_health),
                # GET shows a consent page (no side effect); POST — submitted by
                # the Confirm button on that page — actually stores the token.
                # Splitting them means a credential is never saved without an
                # explicit user action on a page that names the identities.
                web.get("/auth/callback", self._handle_consent),
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

    def _validate(self, request: web.Request) -> tuple[EnrollmentState, str, str]:
        """Validate an enrollment request, or raise an HTML error response.

        Returns ``(enrollment, subject_token, browser_email)`` on success. On
        failure it raises an ``aiohttp`` ``HTTPException`` (which is itself the
        HTML error response), so the handlers stay linear. No side effects —
        run identically on the GET consent view and the POST that stores.
        """
        # 1. Verify the signed state — binds this browser session to the Slack
        #    (team, user, email) that requested enrollment. Reject anything
        #    unsigned, tampered, or expired before touching the forwarded token.
        secret = self._settings.databricks_state_secret or ""
        try:
            enrollment = verify_state(request.query.get("state", ""), secret)
        except StateError as exc:
            _logger.info("Enrollment state rejected: %s", exc)
            raise _error(400, str(exc)) from exc

        # 2. The proxy authenticated the browser user and forwarded their token
        #    and identity. Absence means the bot isn't behind the Apps proxy
        #    with user authorization enabled — an operator misconfig.
        subject_token = request.headers.get(FORWARDED_ACCESS_TOKEN_HEADER)
        browser_email = request.headers.get(FORWARDED_EMAIL_HEADER)
        if not subject_token or not browser_email:
            _logger.warning(
                "Enrollment callback missing forwarded identity headers "
                "(token=%s email=%s) — is user authorization enabled on this app?",
                bool(subject_token),
                bool(browser_email),
            )
            raise _error(
                401,
                "Could not read your Databricks identity. The bot may not be "
                "configured for user authorization — contact your operator.",
            )

        # 3. CONFUSED-DEPUTY GUARD. The signed state proves which Slack user
        #    requested the link; `browser_email` (proxy-authenticated) proves who
        #    actually opened it. They must be the same person — otherwise a link
        #    bound to Slack user A, opened by victim V, would store V's token
        #    under A. Require the browser's email to equal the email Slack
        #    reported for the requesting user (baked into the signed state).
        if not emails_match(browser_email, enrollment.email):
            _logger.warning(
                "Enrollment identity mismatch team=%s user=%s "
                "state_email=%s browser_email=%s — refusing to store token",
                enrollment.team_id,
                enrollment.user_id,
                enrollment.email,
                browser_email,
            )
            raise _error(
                403,
                "This sign-in link was issued for a different account. "
                "Start again from Slack and sign in as yourself.",
            )
        return enrollment, subject_token, browser_email

    async def _handle_consent(self, request: web.Request) -> web.Response:
        """GET: validate, then show a consent page. Stores nothing.

        The token is only saved after the user clicks Confirm (which POSTs back
        to this same URL). Showing consent before saving is essential: the
        browser user is whoever the proxy authenticated, which may not be the
        person who was handed the link — so we ask them to confirm *their own*
        identities before any credential is persisted.
        """
        enrollment, _token, browser_email = self._validate(request)
        return _html_response(
            _consent_page(
                state=request.query.get("state", ""),
                server_url=self._settings.server_url,
                idp_email=browser_email,
                slack_email=enrollment.email,
                team_name=enrollment.team_name,
            )
        )

    async def _handle_confirm(self, request: web.Request) -> web.Response:
        """POST: re-validate and store the forwarded token as the user's bearer.

        Re-runs the full validation (never trust that a GET happened first), then
        persists the forwarded token. Databricks' OBO contract is "pass the
        forwarded access token straight through" — an audience-scoped token
        exchange is NOT available (the workspace rejects `audience` for an
        access_token subject). Least-privilege comes from this app's
        `user_api_scopes`. Empty refresh_token: the ~1h token stands alone until
        it expires, then the bot re-prompts enrollment (same model as OIDC
        session JWTs — no refresh token is ever stored).
        """
        enrollment, subject_token, browser_email = self._validate(request)
        server_url = self._settings.server_url
        await self._tokens.put(
            enrollment.team_id,
            enrollment.user_id,
            server_url,
            access_token=subject_token,
            refresh_token="",
        )
        if self._on_enrolled is not None:
            await self._on_enrolled(enrollment.team_id, enrollment.user_id, server_url)
        _logger.info(
            "Enrolled Slack user via Databricks web-auth team=%s user=%s server=%s",
            enrollment.team_id,
            enrollment.user_id,
            server_url,
        )
        return _html_response(
            _success_page(
                server_url=server_url,
                idp_email=browser_email,
                slack_email=enrollment.email,
                team_name=enrollment.team_name,
            )
        )


def _html_response(body: str, *, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/html")


# Status → aiohttp exception for the error responses the callback can raise.
_HTTP_ERRORS: dict[int, type[web.HTTPException]] = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
}


def _error(status: int, reason: str) -> web.HTTPException:
    """Build a raisable HTML error response with a friendly page body."""
    return _HTTP_ERRORS[status](text=_error_page(reason), content_type="text/html")


def _identity_summary(
    *, verb: str, server_url: str, idp_email: str, slack_email: str, team_name: str
) -> str:
    """Escaped one-sentence description of the link being made.

    ``verb`` is "about to connect" (consent) or "connected" (success). All
    interpolated values are HTML-escaped (they come from the request / Slack).
    """
    workspace = f" in Slack workspace <b>{html.escape(team_name)}</b>" if team_name else ""
    return (
        f"You are {verb} your Omnigent <b>{html.escape(server_url)}</b> account "
        f"<b>{html.escape(idp_email)}</b> with Slack user "
        f"<b>{html.escape(slack_email)}</b>{workspace}."
    )


def _consent_page(
    *, state: str, server_url: str, idp_email: str, slack_email: str, team_name: str
) -> str:
    # Shown BEFORE anything is stored. The browser user is whoever the proxy
    # authenticated — which may not be the person handed the link — so we name
    # the exact identities and require an explicit Confirm (a POST) before the
    # token is saved. The form posts back to the same URL, carrying the signed
    # state; the button is the only way to persist a credential.
    summary = _identity_summary(
        verb="about to connect",
        server_url=server_url,
        idp_email=idp_email,
        slack_email=slack_email,
        team_name=team_name,
    )
    message = (
        f"{summary}<br><br>"
        "Only continue if <b>all three</b> are correct and this is you. If any of "
        "the above is unrecognized, do <b>NOT</b> confirm — doing so lets that "
        "Slack user act as you and use your Omnigent account. Close this tab "
        "instead.<br><br>"
        f'<form method="post" action="/auth/callback?state={html.escape(state)}">'
        '<button type="submit" style="font-size:1rem;padding:0.6rem 1.2rem;'
        'border:0;border-radius:6px;background:#1a1a1a;color:#fff;cursor:pointer">'
        "Confirm &amp; connect</button></form>"
    )
    return _page("Confirm your Omnigent connection", message)


def _success_page(*, server_url: str, idp_email: str, slack_email: str, team_name: str) -> str:
    summary = _identity_summary(
        verb="connected",
        server_url=server_url,
        idp_email=idp_email,
        slack_email=slack_email,
        team_name=team_name,
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
