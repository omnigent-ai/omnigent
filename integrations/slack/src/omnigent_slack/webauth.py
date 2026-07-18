"""Enrollment web server for the Databricks Apps web-auth flow.

Runs inside the bot process as a second Databricks App (user authorization
enabled). It serves two routes behind the Databricks Apps proxy:

- ``GET /`` / ``GET /health`` — liveness for the platform.
- ``GET /auth/callback?state=<signed>`` — the enrollment landing. The proxy has
  already authenticated the browser user and injected
  ``x-forwarded-access-token``; this route verifies the signed ``state`` (which
  binds the session to the Slack identity that requested it), exchanges the
  forwarded token for one scoped to the target Omnigent app, and stores it for
  that ``(team, user, server)`` so the Socket-Mode bot can act as the user.

The heavy lifting (state signing, token exchange) lives in
:mod:`omnigent_slack.databricks_auth`; this module is just the aiohttp wiring
and the two tiny HTML responses. See ``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from omnigent_slack.config import Settings
from omnigent_slack.databricks_auth import (
    FORWARDED_ACCESS_TOKEN_HEADER,
    StateError,
    TokenExchangeError,
    exchange_token,
    sign_state,
    verify_state,
)
from omnigent_slack.tokens import TokenStore

_logger = logging.getLogger(__name__)

# Fired after a user's app-scoped token is stored, with (team_id, user_id,
# server_url) — same contract as AuthManager's hook, so the client pool drops
# any stale tokenless client and rebuilds with the fresh token.
EnrolledHook = Callable[[str, str, str], Awaitable[None]]


class WebAuthServer:
    """aiohttp app serving the Databricks enrollment landing page.

    :param settings: Loaded bot settings (databricks mode + exchange knobs).
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

    def enrollment_url(self, team_id: str, user_id: str) -> str | None:
        """Build the signed enrollment link to post into Slack, or ``None``.

        ``None`` when the public base URL isn't configured (no
        ``OMNIGENT_SLACK_WEBAUTH_BASE_URL`` / ``DATABRICKS_APP_URL``), so the
        caller can surface a clear operator-facing message instead of a broken
        link.
        """
        base = self._settings.webauth_base_url
        secret = self._settings.databricks_state_secret
        if not base or not secret:
            return None
        state = sign_state(team_id, user_id, secret)
        return f"{base}/auth/callback?state={state}"

    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_health),
                web.get("/health", self._handle_health),
                web.get("/auth/callback", self._handle_callback),
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

    async def _handle_callback(self, request: web.Request) -> web.Response:
        # 1. Verify the signed state — binds this browser session to the Slack
        #    (team, user) that requested enrollment. Reject anything unsigned,
        #    tampered, or expired before touching the forwarded token.
        state = request.query.get("state", "")
        secret = self._settings.databricks_state_secret or ""
        try:
            enrollment = verify_state(state, secret)
        except StateError as exc:
            _logger.info("Enrollment state rejected: %s", exc)
            return _html_response(_error_page(str(exc)), status=400)

        # 2. The proxy authenticated the browser user and forwarded their token.
        #    Its absence means the bot isn't actually behind the Apps proxy with
        #    user authorization enabled — an operator misconfiguration.
        subject_token = request.headers.get(FORWARDED_ACCESS_TOKEN_HEADER)
        if not subject_token:
            _logger.warning(
                "Enrollment callback missing %s — is user authorization enabled on this app?",
                FORWARDED_ACCESS_TOKEN_HEADER,
            )
            return _html_response(
                _error_page(
                    "Could not read your Databricks identity. The bot may not be "
                    "configured for user authorization — contact your operator."
                ),
                status=401,
            )

        workspace_host = self._settings.workspace_host
        audience = self._settings.databricks_target_audience
        if not workspace_host or not audience:
            _logger.error(
                "Enrollment misconfigured: workspace_host=%s audience_set=%s",
                workspace_host,
                bool(audience),
            )
            return _html_response(
                _error_page("Enrollment is not fully configured. Contact your operator."),
                status=500,
            )

        # 3. Exchange the forwarded token for one scoped to the target app.
        try:
            exchanged = await exchange_token(
                workspace_host=workspace_host,
                subject_token=subject_token,
                audience=audience,
                scope=self._settings.exchange_scope,
                subject_token_type=self._settings.subject_token_type,
            )
        except TokenExchangeError as exc:
            _logger.info("Token exchange failed: %s", exc)
            return _html_response(
                _error_page(
                    "Could not complete sign-in with Databricks. You may not have "
                    "access to this Omnigent app. Try again or contact your operator."
                ),
                status=502,
            )

        # 4. Store the app-scoped token for the Slack identity from the state.
        #    Empty refresh_token: the exchanged token stands alone until expiry,
        #    then the bot re-prompts enrollment (same path as OIDC session JWTs
        #    in auth_manager — a broad refresh token never touches the store).
        server_url = self._settings.server_url
        await self._tokens.put(
            enrollment.team_id,
            enrollment.user_id,
            server_url,
            access_token=exchanged.access_token,
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
        return _html_response(_success_page())


def _html_response(body: str, *, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/html")


def _success_page() -> str:
    return _page(
        "You're connected",
        "You can close this tab and return to Slack — mention the bot to start.",
    )


def _error_page(reason: str) -> str:
    return _page("Sign-in didn't complete", reason)


def _page(title: str, message: str) -> str:
    # Minimal self-contained page — no external assets (the app runs behind a
    # locked-down proxy). ``title``/``message`` are our own copy, not user input.
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        "padding:0 1rem;line-height:1.5;color:#1a1a1a}h1{font-size:1.4rem}</style>"
        f"</head><body><h1>{title}</h1><p>{message}</p></body></html>"
    )
