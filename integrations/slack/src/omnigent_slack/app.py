from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from omnigent_slack.config import load_settings
from omnigent_slack.omnigent import OmnigentAuth, OmnigentClient
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.store import SQLiteStore


async def run() -> None:
    load_dotenv()
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)
    logger.info(
        "Starting Omnigent Slack bot base_url=%s database=%s runner_workspace=%s",
        settings.omnigent_base_url,
        settings.database_path,
        settings.omnigent_runner_workspace,
    )

    store = SQLiteStore(settings.database_path)
    await store.initialize()

    omnigent = OmnigentClient(
        base_url=str(settings.omnigent_base_url),
        auth=OmnigentAuth(
            email=settings.omnigent_auth_email,
            header_name=settings.omnigent_auth_header_name,
            session_cookie=settings.omnigent_session_cookie,
        ),
        runner_workspace=settings.omnigent_runner_workspace,
        runner_host_id=settings.omnigent_runner_host_id,
        runner_launch_timeout_seconds=settings.omnigent_runner_launch_timeout_seconds,
    )
    logger.info("Checking Omnigent server availability base_url=%s", settings.omnigent_base_url)
    try:
        agents = await omnigent.list_agents()
    except Exception:
        logger.exception(
            "Omnigent server is not reachable at %s; aborting startup", settings.omnigent_base_url
        )
        await omnigent.aclose()
        raise
    logger.info("Omnigent server is up; found %s built-in agents", len(agents))

    agent_id = _resolve_agent_id(agents, settings.omnigent_agent_name)
    if agent_id is None:
        available = ", ".join(sorted(str(a.get("name")) for a in agents if a.get("name"))) or "none"
        await omnigent.aclose()
        raise RuntimeError(
            f"No Omnigent agent named {settings.omnigent_agent_name!r} was found. "
            f"Available agents: {available}"
        )
    logger.info("Resolved Omnigent agent name=%s to id=%s", settings.omnigent_agent_name, agent_id)

    service = SlackOmnigentService(
        store=store,
        omnigent=omnigent,
        omnigent_agent_id=agent_id,
        update_interval_seconds=settings.slack_update_interval_seconds,
    )

    app = AsyncApp(token=settings.slack_bot_token)
    register_handlers(app, service)

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    try:
        logger.info("Connecting to Slack Socket Mode")
        await handler.start_async()  # type: ignore[no-untyped-call]
    finally:
        logger.info("Shutting down Omnigent Slack bot")
        await service.shutdown()
        await omnigent.aclose()


def _resolve_agent_id(agents: list[dict[str, Any]], agent_name: str) -> str | None:
    for agent in agents:
        if agent.get("name") == agent_name:
            agent_id = agent.get("id")
            if isinstance(agent_id, str):
                return agent_id
    return None


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
