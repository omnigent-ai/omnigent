from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from omnigent_slack.approvals import (
    ACTION_APPROVE,
    ACTION_DENY,
    ACTION_FORM_ANSWER,
    ACTION_FORM_CANCEL,
    ACTION_FORM_SUBMIT,
    route_elicitation_click,
)
from omnigent_slack.auth_manager import AuthManager, pack_user_key
from omnigent_slack.config import load_settings
from omnigent_slack.databricks_oauth import DatabricksOAuthClient
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.setup import SetupFlow
from omnigent_slack.store import SQLiteStore
from omnigent_slack.tokens import EncryptedTokenStore, InMemoryTokenStore, TokenStore
from omnigent_slack.webauth import WebAuthServer


async def run() -> None:
    load_dotenv()
    settings = load_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    # force=True so this wins even when an entry point (e.g. the Databricks App
    # wrapper) already called basicConfig at import — otherwise a second
    # basicConfig is a no-op and LOG_LEVEL is silently ignored, pinning us to
    # whatever the first call set and hiding slack_sdk's connection diagnostics.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # The Slack SDK/Bolt loggers carry the connection diagnostics (DNS, TLS,
    # websocket handshake) needed to diagnose an outbound-egress failure. Pin
    # them to the configured level so LOG_LEVEL=DEBUG actually surfaces them.
    for name in ("slack_sdk", "slack_bolt"):
        logging.getLogger(name).setLevel(level)
    logger = logging.getLogger(__name__)
    logger.info(
        "Starting Omnigent Slack bot server=%s database=%s",
        settings.server_url,
        settings.database_path,
    )

    store = SQLiteStore(settings.database_path)
    await store.initialize()

    # Delegated auth (RFC 8628): per-user tokens for auth-enabled servers.
    # With an encryption key, tokens persist to disk encrypted at rest. Without
    # one, they live only in memory — the integration still works, but tokens
    # are lost on restart so users re-authenticate. We never write bearer
    # credentials to disk in the clear.
    token_store: TokenStore
    if settings.token_encryption_key:
        token_store = EncryptedTokenStore(settings.database_path, settings.token_encryption_key)
    else:
        logger.warning(
            "OMNIGENT_SLACK_TOKEN_ENCRYPTION_KEY not set — delegated tokens will "
            "be kept in memory only and lost on restart (users re-authenticate). "
            "Set the key to persist them encrypted at rest."
        )
        token_store = InMemoryTokenStore()
    await token_store.initialize()

    # The bot talks to one operator-configured Omnigent server
    # (settings.server_url) — never a user-supplied URL. The pool holds one
    # client per (server, packed-user) carrying that user's delegated bearer
    # token. Created first so the auth manager can invalidate a cached client
    # the moment a token is stored/removed (login/logout).
    pool = OmnigentClientPool()

    async def _on_token_changed(team_id: str, user_id: str, server_url: str) -> None:
        await pool.invalidate(server_url, pack_user_key(team_id, user_id))

    # Databricks web-auth mode: the server is fronted by the Databricks Apps
    # proxy, which the device/OIDC probe can't drive. Instead the bot runs its
    # own custom U2M OAuth app (authorization code + PKCE, offline_access) via an
    # enrollment page it serves as a Databricks App: a Slack user signs in and
    # the bot stores the resulting durable, refreshable token as their bearer.
    # The OAuth client is shared as the AuthManager's rotator so refresh/revoke
    # hit the workspace, not the Omnigent server. The web server shares the token
    # store, so a token it writes is immediately usable by the bot.
    webauth: WebAuthServer | None = None
    enrollment_url = None
    rotator: DatabricksOAuthClient | None = None
    if settings.server_auth_mode == "databricks":
        rotator = DatabricksOAuthClient(
            settings.databricks_workspace_host or "",
            client_id=settings.databricks_oauth_client_id or "",
            client_secret=settings.databricks_oauth_client_secret or "",
            scopes=settings.databricks_oauth_scopes_normalized,
            redirect_uri=settings.databricks_redirect_uri,
        )
        webauth = WebAuthServer(
            settings, token_store, on_enrolled=_on_token_changed, oauth_client=rotator
        )
        enrollment_url = webauth.enrollment_url

    auth_manager = AuthManager(
        token_store,
        on_token_changed=_on_token_changed,
        client_secret=settings.device_client_secret,
        rotator=rotator,
    )
    pool.set_auth_resolver(auth_manager.resolve_auth)

    setup = SetupFlow(
        store=store,
        pool=pool,
        server_url=settings.server_url,
        auth_manager=auth_manager,
        enrollment_url=enrollment_url,
    )
    service = SlackOmnigentService(
        store=store,
        pool=pool,
        setup=setup,
        server_url=settings.server_url,
    )

    app = AsyncApp(token=settings.slack_bot_token)
    setup.register(app)
    register_handlers(app, service)
    _register_error_handler(app, logger)

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    try:
        # Inside the try so a webauth-start failure still runs the finally cleanup
        # (store/pool/auth_manager close, webauth.stop is idempotent).
        if webauth is not None:
            await webauth.start()
        logger.info("Connecting to Slack Socket Mode")
        await handler.start_async()  # type: ignore[no-untyped-call]
    except Exception:
        # The initial reach-out to Slack (apps.connections.open over HTTPS, then
        # the wss:// socket) is the most likely outbound failure — restricted
        # egress, DNS, or a bad app token. Log it explicitly with a traceback so
        # it's not swallowed into a generic "failed to start" upstream.
        logger.exception("Could not connect to Slack Socket Mode")
        raise
    finally:
        logger.info("Shutting down Omnigent Slack bot")
        await service.shutdown()
        # Cancel any in-flight login/enrollment poll tasks (and their httpx
        # clients) so they aren't abandoned mid-poll.
        await auth_manager.shutdown()
        await pool.aclose_all()
        if webauth is not None:
            await webauth.stop()


def _register_error_handler(app: AsyncApp, logger: logging.Logger) -> None:
    """Log any listener/API error Bolt would otherwise swallow at DEBUG.

    Without a registered handler, Bolt logs unhandled listener exceptions at a
    level that's easy to miss and returns a generic ack. This surfaces every
    one (including outbound Web API failures inside a handler) with a traceback.
    """

    @app.error
    async def _on_error(error: Exception, body: dict[str, Any]) -> None:
        logger.exception("Unhandled Slack listener error; body_type=%s", body.get("type"))


def register_handlers(app: AsyncApp, service: SlackOmnigentService) -> None:
    @app.event("app_mention")
    async def handle_app_mention(
        body: dict[str, Any],
        event: dict[str, Any],
        client: Any,
        context: dict[str, Any],
    ) -> None:
        await service.handle_app_mention(body=body, event=event, client=client, context=context)

    @app.event("message")
    async def handle_message(
        body: dict[str, Any],
        event: dict[str, Any],
        client: Any,
        context: dict[str, Any],
    ) -> None:
        if not body.get("team_id") and not event.get("team"):
            return
        await service.handle_message(body=body, event=event, client=client, context=context)

    @app.action(ACTION_APPROVE)
    async def handle_approve(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await route_elicitation_click(service, client, body, accepted=True)

    @app.action(ACTION_DENY)
    async def handle_deny(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await route_elicitation_click(service, client, body, accepted=False)

    @app.action(ACTION_FORM_SUBMIT)
    async def handle_form_submit(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await route_elicitation_click(service, client, body, accepted=True, is_form_submit=True)

    @app.action(ACTION_FORM_CANCEL)
    async def handle_form_cancel(ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        await route_elicitation_click(service, client, body, accepted=False, is_form_submit=True)

    @app.action(ACTION_FORM_ANSWER)
    async def handle_form_answer(ack: Any) -> None:
        # Radio/checkbox selection changes are read from state.values at submit
        # time; ack each change so Slack doesn't flag an unhandled interaction.
        await ack()
