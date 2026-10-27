"""Gateway wiring: the discord.py client, its intents, and the slash commands.

Everything Discord-shaped that isn't a component lives here. The bot uses a
bare :class:`discord.Client` plus an application-command tree rather than
``commands.Bot`` — it has no prefix commands, so the extension machinery would
be dead weight.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import discord
from discord import app_commands
from omnigent_bot_core.omnigent import OmnigentClientPool

from omnigent_discord.auth_manager import AuthManager
from omnigent_discord.config import ConfigError, Settings, load_settings
from omnigent_discord.notifications import DiscordNotifier
from omnigent_discord.service import DiscordOmnigentService
from omnigent_discord.setup import COMMAND_NAME, SetupFlow
from omnigent_discord.store import SQLiteStore
from omnigent_discord.tokens import EncryptedTokenStore, InMemoryTokenStore, TokenStore


def build_intents() -> discord.Intents:
    """The gateway intents the bot needs, and only those.

    ``message_content`` is **privileged**: it must be enabled on the app's Bot
    page in the Discord developer portal. Because the bot REQUESTS it here,
    Discord refuses the connection outright when it is off — the process exits
    at startup with ``PrivilegedIntentsRequired`` rather than coming online and
    going quiet. ``members`` and ``presences`` are deliberately left off — the
    bot never needs a member list.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.message_content = True
    return intents


class OmnigentClient(discord.Client):
    """A Discord client that routes messages into Omnigent sessions."""

    def __init__(self, settings: Settings, service: DiscordOmnigentService) -> None:
        # Every message this bot posts carries agent- or user-authored text
        # (streamed answer deltas, todo plans, approval-card bodies). Without an
        # explicit policy Discord parses mentions out of that raw content, so a
        # prompt could make the agent emit a role mention and ping the whole
        # server. Deny by default; the one deliberate ping passes its own
        # allowance (see ``DiscordNotifier.post_private``).
        super().__init__(intents=build_intents(), allowed_mentions=discord.AllowedMentions.none())
        self._settings = settings
        self._service = service
        self._logger = logging.getLogger(__name__)
        self.tree = app_commands.CommandTree(self)
        # ``on_ready`` fires again after every gateway resume, so the one-time
        # command sync is guarded rather than repeated on each reconnect.
        self._commands_synced = False

    async def on_ready(self) -> None:
        if self.user is not None:
            self._service.set_bot_user_id(str(self.user.id))
            self._logger.info("Connected to Discord as %s (%s)", self.user, self.user.id)
        if not self._commands_synced:
            await self._sync_commands()
            self._commands_synced = True

    async def _sync_commands(self) -> None:
        """Publish ``/omnigent`` globally, or to the opt-in fast-iteration guilds.

        Global is the default because Discord routes DM interactions only to
        globally-registered commands, and the DM flow needs ``/omnigent new``
        (a DM has no threads, so that command is its only reset). Registering
        per-guild is faster to appear but DM-blind, so it is opt-in via
        ``OMNIGENT_DISCORD_COMMAND_GUILD_IDS`` and warns about the trade.
        """
        guild_ids = self._settings.command_guild_ids
        current = ""
        try:
            if guild_ids:
                # Explicit opt-in for fast iteration: a guild command appears in
                # seconds. The trade is that it exists ONLY in those guilds —
                # Discord routes DM interactions to globally-registered commands
                # only — so /omnigent is unavailable in DMs in this mode.
                for guild_id in guild_ids:
                    current = guild_id
                    guild = discord.Object(id=int(guild_id))
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                self._logger.warning(
                    "Registered /%s in %d guild(s) only — it will NOT appear in DMs. "
                    "Unset OMNIGENT_DISCORD_COMMAND_GUILD_IDS for global registration.",
                    COMMAND_NAME,
                    len(guild_ids),
                )
            else:
                # The default. A global command works in every guild AND in DMs,
                # which the DM flow depends on: /omnigent new is the only way to
                # end a DM session, since Discord DMs have no threads.
                await self.tree.sync()
                self._logger.info(
                    "Registered /%s globally — allow a few minutes to appear", COMMAND_NAME
                )
        except discord.Forbidden:
            # Registering a guild command needs the app authorized in that guild
            # with `applications.commands`. A `bot`-only invite connects to the
            # gateway perfectly well and then 403s here, so name the fix rather
            # than leaving a bare traceback. Same 403 when the bot was never
            # added to the guild the operator configured.
            self._logger.error(
                "Discord refused to register /%s in guild %s. Re-invite the bot with "
                "BOTH the `bot` and `applications.commands` scopes (a bot-only invite "
                "connects fine but cannot register commands), and check the guild id in "
                "OMNIGENT_DISCORD_GUILD_IDS is one the bot has been added to. "
                "Mentions and DMs keep working meanwhile; only the slash command is "
                "missing.",
                COMMAND_NAME,
                current,
            )
        except Exception:
            # A failed sync leaves the bot able to answer mentions but with no
            # slash command, so make the cause loud rather than silent.
            self._logger.exception("Could not register the /%s command", COMMAND_NAME)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return
        await self._service.handle_message(message)

    async def dm_channel_for(self, user_id: str) -> discord.abc.Messageable | None:
        """Open (or reuse) the DM channel with ``user_id``.

        Used for notices that shouldn't address the whole channel. Returns
        ``None`` when the user can't be reached — most often because their DMs
        are closed to server members, which is common and not an error.
        """
        try:
            user = self.get_user(int(user_id)) or await self.fetch_user(int(user_id))
        except (ValueError, discord.HTTPException):
            return None
        if user is None:
            return None
        try:
            return user.dm_channel or await user.create_dm()
        except discord.HTTPException:
            return None


def register_commands(
    client: OmnigentClient, setup: SetupFlow, service: DiscordOmnigentService
) -> None:
    """Add the ``/omnigent`` command group to the client's tree.

    Three subcommands mirror the Slack integration's ``/omnigent`` verbs, plus
    ``new`` — which Discord needs because a DM has no threads and would
    otherwise stay bound to one session forever.
    """
    group = app_commands.Group(
        name=COMMAND_NAME,
        description="Configure and control your Omnigent sessions.",
        # DMs are a first-class entry point, so the command must be usable there
        # as well as in a guild.
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @group.command(name="config", description="Choose your Omnigent agent, host, and workspace.")
    async def config_command(interaction: discord.Interaction) -> None:
        await setup.run_config(interaction)

    @group.command(name="new", description="Start a fresh Omnigent session in this conversation.")
    async def new_command(interaction: discord.Interaction) -> None:
        await service.start_new_session(interaction)

    @group.command(name="logout", description="Sign out of Omnigent and clear your settings.")
    async def logout_command(interaction: discord.Interaction) -> None:
        await setup.run_logout(interaction)

    client.tree.add_command(group)

    @client.tree.error
    async def on_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # Without a handler discord.py logs the traceback and leaves the user
        # staring at a spinner, so always close the interaction with something.
        logging.getLogger(__name__).exception(
            "Unhandled /%s command error", COMMAND_NAME, exc_info=error
        )
        message = "Something went wrong running that command. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


def _build_token_store(settings: Settings, logger: logging.Logger) -> TokenStore:
    """Pick the token backend for the configured encryption key.

    With a key, tokens persist to disk encrypted at rest. Without one they live
    only in memory — the integration still works, but tokens are lost on restart
    so users re-authenticate. We never write bearer credentials to disk in the
    clear.
    """
    if settings.token_encryption_key:
        return EncryptedTokenStore(settings.database_path, settings.token_encryption_key)
    logger.warning(
        "OMNIGENT_DISCORD_TOKEN_ENCRYPTION_KEY not set — delegated tokens will be "
        "kept in memory only and lost on restart (users re-authenticate). Set the "
        "key to persist them encrypted at rest."
    )
    return InMemoryTokenStore()


async def run() -> None:
    """Start the bot and block until the gateway connection ends.

    Config comes from real environment variables only — mirroring ``omni
    server`` (the core CLI loads no .env). Whatever populates the environment
    (your shell, ``uv run``, the container deploy) is the single source of
    truth. See integrations/discord/README.
    """
    # A missing/invalid config raises ConfigError with an operator-friendly,
    # pre-formatted message. Print it plainly and exit non-zero — no traceback,
    # no logging setup (which hasn't run yet). SystemExit(2) is the conventional
    # "usage/config" exit code and is what the foreground CLI surfaces.
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"omnigent-discord: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    # force=True so this wins even when an entry point already called
    # basicConfig at import — otherwise a second basicConfig is a no-op and
    # LOG_LEVEL is silently ignored, hiding discord.py's connection diagnostics.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # discord.py's loggers carry the connection diagnostics (DNS, TLS, gateway
    # handshake, rate limits) needed to diagnose an outbound-egress failure.
    for name in ("discord", "discord.gateway", "discord.http"):
        logging.getLogger(name).setLevel(level)
    logger = logging.getLogger(__name__)
    logger.info(
        "Starting Omnigent Discord bot server=%s database=%s",
        settings.server_url,
        settings.database_path,
    )

    store = SQLiteStore(settings.database_path)
    await store.initialize()

    token_store = _build_token_store(settings, logger)
    await token_store.initialize()

    # The bot talks to one operator-configured Omnigent server
    # (settings.server_url) — never a user-supplied URL. The pool holds one
    # client per (server, discord user) carrying that user's delegated bearer
    # token. Created first so the auth manager can invalidate a cached client
    # the moment a token is stored/removed (login/logout).
    pool = OmnigentClientPool()

    async def _on_token_changed(user_id: str, server_url: str) -> None:
        await pool.invalidate(server_url, user_id)

    auth_manager = AuthManager(
        token_store,
        on_token_changed=_on_token_changed,
        client_secret=settings.device_client_secret,
    )
    pool.set_auth_resolver(auth_manager.resolve_auth)

    setup = SetupFlow(
        store=store,
        pool=pool,
        server_url=settings.server_url,
        auth_manager=auth_manager,
    )

    # The notifier needs a DM resolver that only exists once the client does, so
    # it is wired through a mutable holder rather than a constructor cycle.
    client_holder: dict[str, OmnigentClient] = {}

    async def _dm_resolver(user_id: str) -> Any | None:
        client = client_holder.get("client")
        return await client.dm_channel_for(user_id) if client is not None else None

    notifier = DiscordNotifier(
        server_url=settings.server_url, logger=logger, dm_resolver=_dm_resolver
    )
    service = DiscordOmnigentService(
        store=store,
        pool=pool,
        notifier=notifier,
        server_url=settings.server_url,
        guild_allowed=settings.guild_allowed,
        stream_edit_interval_seconds=settings.stream_edit_interval_seconds,
        allow_file_upload=settings.allow_file_upload,
        max_attachment_bytes=settings.max_attachment_bytes,
        max_attachments_per_message=settings.max_attachments_per_message,
    )

    client = OmnigentClient(settings, service)
    client_holder["client"] = client
    register_commands(client, setup, service)

    try:
        logger.info("Connecting to the Discord gateway")
        async with client:
            await client.start(settings.bot_token)
    except discord.PrivilegedIntentsRequired:
        # By far the most common first-run failure, and the message discord.py
        # raises doesn't name the fix in the portal.
        logger.exception(
            "Discord rejected the connection: enable the MESSAGE CONTENT INTENT "
            "for this app under Developer Portal → Bot → Privileged Gateway Intents"
        )
        raise
    except discord.LoginFailure:
        logger.exception("Discord rejected the bot token (check OMNIGENT_DISCORD_BOT_TOKEN)")
        raise
    except Exception:
        # The initial reach-out to Discord (HTTPS to /gateway/bot, then the wss
        # socket) is the most likely outbound failure — restricted egress, DNS,
        # or a bad token. Log it with a traceback so it isn't swallowed upstream.
        logger.exception("Could not connect to the Discord gateway")
        raise
    finally:
        logger.info("Shutting down Omnigent Discord bot")
        await service.shutdown()
        # Cancel any in-flight login poll tasks (and their httpx clients) so
        # they aren't abandoned mid-poll.
        await auth_manager.shutdown()
        await pool.aclose_all()
