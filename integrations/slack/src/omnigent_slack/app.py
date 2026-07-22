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
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.setup import SetupFlow
from omnigent_slack.store import SQLiteStore
from omnigent_slack.tokens import EncryptedTokenStore, InMemoryTokenStore, TokenStore


async def run() -> None:
    load_dotenv()
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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

    auth_manager = AuthManager(
        token_store,
        on_token_changed=_on_token_changed,
        client_secret=settings.device_client_secret,
    )
    pool.set_auth_resolver(auth_manager.resolve_auth)
    setup = SetupFlow(
        store=store, pool=pool, server_url=settings.server_url, auth_manager=auth_manager
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

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    try:
        logger.info("Connecting to Slack Socket Mode")
        await handler.start_async()  # type: ignore[no-untyped-call]
    finally:
        logger.info("Shutting down Omnigent Slack bot")
        await service.shutdown()
        await pool.aclose_all()


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
