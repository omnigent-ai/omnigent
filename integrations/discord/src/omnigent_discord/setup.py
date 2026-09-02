"""Per-user Omnigent setup, driven by the ``/omnigent`` application command.

Discord's equivalent of Slack's setup modal is an **ephemeral interaction
response** the bot edits in place: "Connecting…" → (if the server needs auth) a
sign-in link → agent/host selects → a workspace modal → a saved confirmation.
Two Discord constraints shape the flow:

- A modal opens only in response to an interaction, never as the first reply to
  a command that has already been deferred. So the agent and host are chosen
  with select menus on the ephemeral message, and the Save button — a fresh
  interaction — opens the modal that collects the workspace path. The result is
  two steps where the Slack sibling shows one form. (``discord.ui.Label``, added
  in discord.py 2.6, suggests a modern modal can also hold selects, which would
  allow a single form; untested here, so the flow is left as built.)
- An interaction token is valid for 15 minutes, which comfortably covers a
  device-grant login (the code itself expires in 10).

The content builders are pure and return :class:`~omnigent_discord.approvals.Card`
values so the copy is testable without a gateway; ``views``' ``to_embed``
renders them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import discord
from omnigent_bot_core.events import host_id_of
from omnigent_bot_core.oauth import DeviceGrantUnavailableError, OAuthError
from omnigent_bot_core.omnigent import (
    AuthRequiredError,
    OmnigentClient,
    OmnigentClientPool,
    OmnigentError,
    ServerUnreachableError,
    ValidatedServer,
)

from omnigent_discord.approvals import (
    COLOR_NEGATIVE,
    COLOR_NEUTRAL,
    COLOR_POSITIVE,
    Card,
)
from omnigent_discord.auth_manager import AuthManager, discord_client_id
from omnigent_discord.models import UserConfig
from omnigent_discord.store import SQLiteStore
from omnigent_discord.text import MAX_SELECT_OPTIONS, truncate_option
from omnigent_discord.views import to_embed

# The application command users run to configure, reset, or sign out.
COMMAND_NAME = "omnigent"

_logger = logging.getLogger(__name__)


def default_workspace() -> str:
    """Fallback default when the host's home directory can't be resolved.

    Only meaningful when the bot and host share a machine; the user can
    override it in the workspace modal.
    """
    return str(Path.cwd())


def host_unavailable_text(server_url: str) -> str:
    """Guidance shown when no host is online to run a session.

    Shown both during setup (no online host to pick) and at turn time (the
    chosen host went offline), so the wording stays identical everywhere.
    """
    return (
        "⚠️ No online host is available to run your session.\n"
        "Run this on the machine you want to use, then run `/omnigent config`:\n"
        f"`omni host --server {server_url}`"
    )


# ── card content (pure) ───────────────────────────────────────────────────


def connecting_card() -> Card:
    return Card(
        title="Set up Omnigent",
        description="Connecting to Omnigent…",
        color=COLOR_NEUTRAL,
    )


def login_card(server_url: str, verification_url: str, user_code: str) -> Card:
    """Shown when setup hits an auth-enabled server.

    The link is one-click (code prefilled). Device-grant flows still show the
    short code so the user can confirm it matches the consent page; the
    anti-phishing guarantee is the consent page's forced password re-auth (see
    designs/DEVICE_AUTH.md), not code entry. The OIDC ticket flow has no code.
    """
    code_hint = f"\nConfirm the code shown is **{user_code}**." if user_code else ""
    return Card(
        title="Sign in to Omnigent",
        description=(
            f"**{server_url}** requires login.\n\n"
            f"1. [Open the login page]({verification_url}) and sign in.{code_hint}\n"
            "2. This message updates itself once you're done.\n\n"
            "_Waiting for approval…_"
        ),
        color=COLOR_NEUTRAL,
    )


def login_failed_card(server_url: str, reason: str) -> Card:
    """Terminal screen when login is denied, expires, or errors.

    Only for failures of the sign-in itself — a connectivity or server-side
    failure uses :func:`setup_failed_card`, whose wording doesn't blame a login
    that never happened.
    """
    where = f" to **{server_url}**" if server_url else ""
    return Card(
        title="Setup didn't complete",
        description=(
            f"⚠️ Login{where} didn't complete: {reason}\nRun `/omnigent config` to try again."
        ),
        color=COLOR_NEGATIVE,
    )


def setup_failed_card(server_url: str, reason: str) -> Card:
    """Terminal screen when setup can't get a usable answer from the server.

    Distinct from :func:`login_failed_card`: no login is in play, so saying
    "login didn't complete" sends the operator looking in the wrong place.
    """
    return Card(
        title="Setup didn't complete",
        description=(
            f"⚠️ Couldn't set up against **{server_url}**: {reason}\n"
            "Run `/omnigent config` to try again."
        ),
        color=COLOR_NEGATIVE,
    )


# The two ways the server can fail to answer, kept apart because they send the
# operator to completely different places: a transport failure means nothing is
# there (or the URL is wrong), while an error response means the server IS there
# and something inside it broke. Reporting the second as the first is what makes
# a stale server squatting the expected port look like a server that is down.
SERVER_UNREACHABLE_REASON = (
    "the server couldn't be reached. Check it's running, and that "
    "`OMNIGENT_SERVER_URL` names the right address and port — `omni server "
    "status` prints it, and the local server falls back to a random port when "
    "its default is already taken."
)
SERVER_ERROR_REASON = (
    "the server answered, but with an error. It is reachable, so check its own "
    "logs — and confirm `OMNIGENT_SERVER_URL` points at the server you meant "
    "rather than an older one still holding the port."
)


def no_agents_card(server_url: str) -> Card:
    """Shown when the server exposes no agents — setup can't finish."""
    return Card(
        title="Set up Omnigent",
        description=(
            f"⚠️ **{server_url}** has no agents available.\n"
            "Add an agent on the server, then run `/omnigent config` again."
        ),
        color=COLOR_NEGATIVE,
    )


def no_host_card(server_url: str) -> Card:
    return Card(
        title="Set up Omnigent",
        description=host_unavailable_text(server_url),
        color=COLOR_NEGATIVE,
    )


def select_card(server_url: str) -> Card:
    return Card(
        title="Set up Omnigent",
        description=(
            f"Connected to **{server_url}**.\n"
            "Pick an agent and a host, then choose **Save workspace** to finish."
        ),
        color=COLOR_NEUTRAL,
    )


def saved_card(config: UserConfig, server_url: str) -> Card:
    host_line = f" on host **{config.host_name}**" if config.host_name else ""
    return Card(
        title="✅ You're set up",
        description=(
            f"I'll use **{config.agent_name}**{host_line} on {server_url}, "
            f"rooted at `{config.workspace}`.\n"
            "Mention me in a channel or DM me to start a session."
        ),
        color=COLOR_POSITIVE,
    )


def setup_required_text() -> str:
    """The nudge sent to a user who hasn't configured yet.

    Names a server channel because this often arrives in a DM, where a slash
    command may not be available: a command reaches DMs only once its global
    registration has propagated, and a freshly-started bot has not. Setup is
    per-account, so configuring from a channel applies to the DM too.
    """
    return (
        "👋 Set up Omnigent before we start — run **/omnigent config** in a "
        "server channel and pick an agent, a host, and a workspace. It applies "
        "everywhere, including here."
    )


def relogin_required_text() -> str:
    """The nudge sent to a configured user whose login expired."""
    return (
        "🔒 Your Omnigent login has expired. Run **/omnigent config** to sign in "
        "again and keep going."
    )


def agent_options(agents: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """``(label, value)`` pairs for the agent select, capped to Discord's limit."""
    options: list[tuple[str, str]] = []
    for agent in agents[:MAX_SELECT_OPTIONS]:
        agent_id = agent.get("id")
        if not isinstance(agent_id, str):
            continue
        name = agent.get("name") or agent_id
        options.append((truncate_option(str(name)), agent_id))
    return options


def host_options(hosts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """``(label, value)`` pairs for the host select, capped to Discord's limit."""
    options: list[tuple[str, str]] = []
    for host in hosts[:MAX_SELECT_OPTIONS]:
        host_id = host_id_of(host)
        if host_id is None:
            continue
        name = host.get("name") or host_id
        options.append((truncate_option(str(name)), host_id))
    return options


# ── components ────────────────────────────────────────────────────────────


class _LinkView(discord.ui.View):
    """A single link button — the sign-in link on the login screen."""

    def __init__(self, label: str, url: str) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=label, url=url))


class _SelectionView(discord.ui.View):
    """Agent + host selects and a Save button that opens the workspace modal.

    Only the invoking user can touch it: the message is ephemeral, so nobody
    else can see it, and :meth:`interaction_check` refuses anything else as a
    belt-and-braces guard.
    """

    def __init__(
        self,
        flow: SetupFlow,
        *,
        user_id: str,
        agents: list[tuple[str, str]],
        hosts: list[tuple[str, str]],
        workspace_default: str,
        timeout_seconds: float = 600.0,
    ) -> None:
        super().__init__(timeout=timeout_seconds)
        self._flow = flow
        self._user_id = user_id
        self._workspace_default = workspace_default
        self._agent: tuple[str, str] | None = None
        self._host: tuple[str, str] | None = None

        self._agent_select = discord.ui.Select(
            placeholder="Choose an agent",
            options=[discord.SelectOption(label=label, value=value) for label, value in agents],
            row=0,
        )
        self._agent_select.callback = self._on_agent  # type: ignore[method-assign]
        self._host_select = discord.ui.Select(
            placeholder="Choose a host",
            options=[discord.SelectOption(label=label, value=value) for label, value in hosts],
            row=1,
        )
        self._host_select.callback = self._on_host  # type: ignore[method-assign]
        save = discord.ui.Button(label="Save workspace", style=discord.ButtonStyle.success, row=2)
        save.callback = self._on_save  # type: ignore[method-assign]
        self.add_item(self._agent_select)
        self.add_item(self._host_select)
        self.add_item(save)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self._user_id

    @staticmethod
    def _chosen(select: discord.ui.Select[Any]) -> tuple[str, str] | None:
        if not select.values:
            return None
        value = select.values[0]
        label = next((o.label for o in select.options if o.value == value), value)
        return label, value

    async def _on_agent(self, interaction: discord.Interaction) -> None:
        self._agent = self._chosen(self._agent_select)
        await interaction.response.defer()

    async def _on_host(self, interaction: discord.Interaction) -> None:
        self._host = self._chosen(self._host_select)
        await interaction.response.defer()

    async def _on_save(self, interaction: discord.Interaction) -> None:
        if self._agent is None or self._host is None:
            await interaction.response.send_message(
                "Choose both an agent and a host first.", ephemeral=True
            )
            return
        agent_name, agent_id = self._agent
        host_name, host_id = self._host
        await interaction.response.send_modal(
            _WorkspaceModal(
                self._flow,
                user_id=self._user_id,
                agent_id=agent_id,
                agent_name=agent_name,
                host_id=host_id,
                host_name=host_name,
                workspace_default=self._workspace_default,
            )
        )


class _WorkspaceModal(discord.ui.Modal):
    """The final step: the absolute workspace path on the chosen host."""

    def __init__(
        self,
        flow: SetupFlow,
        *,
        user_id: str,
        agent_id: str,
        agent_name: str,
        host_id: str,
        host_name: str,
        workspace_default: str,
    ) -> None:
        super().__init__(title="Omnigent workspace")
        self._flow = flow
        self._user_id = user_id
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._host_id = host_id
        self._host_name = host_name
        self._workspace = discord.ui.TextInput(
            label="Workspace path",
            placeholder="/absolute/path/on/the/host",
            default=workspace_default,
            required=True,
            max_length=512,
        )
        self.add_item(self._workspace)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        workspace = str(self._workspace.value).strip()
        if not workspace.startswith("/"):
            await interaction.response.send_message(
                "Enter an absolute workspace path (starting with `/`).", ephemeral=True
            )
            return
        config = UserConfig(
            agent_id=self._agent_id,
            agent_name=self._agent_name,
            workspace=workspace,
            host_id=self._host_id,
            host_name=self._host_name,
        )
        await self._flow.save_config(interaction, self._user_id, config)


# ── flow ──────────────────────────────────────────────────────────────────


class SetupFlow:
    """Per-user Omnigent setup for the operator-configured server.

    The bot talks to one fixed Omnigent server (``server_url``, set by the
    operator — never entered by a user), so setup never asks for a URL. Running
    ``/omnigent config`` validates connectivity against that server, logs the
    user in (in the same ephemeral message) if it requires auth, then lets them
    pick an agent, host, and workspace. The result is persisted per Discord user
    id, so it applies in every guild and DM the bot shares with them.
    """

    def __init__(
        self,
        store: SQLiteStore,
        pool: OmnigentClientPool,
        server_url: str,
        auth_manager: AuthManager | None = None,
    ) -> None:
        self._store = store
        self._pool = pool
        self._server_url = server_url
        self._auth = auth_manager
        self._logger = logging.getLogger(__name__)

    async def run_config(self, interaction: Any) -> None:
        """Handle ``/omnigent config``: validate, log in, then let them choose."""
        user_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(embed=to_embed(connecting_card()))
        server_url = self._server_url
        omnigent = await self._pool.get(server_url, user_id)
        try:
            validated = await omnigent.validate()
        except AuthRequiredError:
            # The server needs auth and this user hasn't logged in yet. Login
            # happens inside this same ephemeral message: show the verification
            # link, poll in the background, and advance to agent/host selection
            # the moment the user approves.
            if self._auth is None or not self._auth.enabled:
                await self._show(
                    interaction,
                    login_failed_card(
                        server_url,
                        "this server requires login, which this bot isn't configured "
                        "for. Ask the bot operator to enable it.",
                    ),
                )
                return
            await self._begin_login(interaction, user_id=user_id, server_url=server_url)
            return
        except ServerUnreachableError as exc:
            # Nothing answered at all: the server is down, or the URL is wrong.
            self._logger.info("Setup could not reach server url=%s error=%s", server_url, exc)
            await self._show(interaction, setup_failed_card(server_url, SERVER_UNREACHABLE_REASON))
            return
        except OmnigentError as exc:
            # It answered with an error (a 5xx, a malformed body). Saying
            # "unreachable" here would send the operator hunting for a down
            # server while theirs is up and failing.
            self._logger.info("Setup validation failed url=%s error=%s", server_url, exc)
            await self._show(interaction, setup_failed_card(server_url, SERVER_ERROR_REASON))
            return

        await self._advance_to_select(interaction, omnigent, user_id, validated)

    async def _advance_to_select(
        self,
        interaction: Any,
        omnigent: OmnigentClient,
        user_id: str,
        validated: ValidatedServer,
    ) -> None:
        """Show the agent/host selects, or explain why setup can't finish."""
        server_url = self._server_url
        if not validated.agents:
            await self._show(interaction, no_agents_card(server_url))
            return
        if not validated.online_hosts:
            # A session needs a host to run on, so setup can't finish without
            # one. Show the same guidance a turn does when no host is reachable.
            await self._show(interaction, no_host_card(server_url))
            return
        # Default the workspace to the host's home directory (where runners
        # actually run), not the bot process's cwd. Fall back to the bot's cwd
        # only if the host can't be probed.
        workspace_default = await self._resolve_default_workspace(omnigent, validated.online_hosts)
        view = _SelectionView(
            self,
            user_id=user_id,
            agents=agent_options(validated.agents),
            hosts=host_options(validated.online_hosts),
            workspace_default=workspace_default,
        )
        await interaction.edit_original_response(
            embed=to_embed(select_card(server_url)), view=view
        )

    async def _begin_login(self, interaction: Any, *, user_id: str, server_url: str) -> None:
        """Show the login link and advance this message once login completes."""
        assert self._auth is not None
        guild = getattr(interaction, "guild", None)
        client_id = discord_client_id(str(getattr(guild, "name", "") or ""))
        try:
            pending = await self._auth.authorize(server_url=server_url, client_id=client_id)
        except DeviceGrantUnavailableError as exc:
            self._logger.info("Device grant unavailable server=%s error=%s", server_url, exc)
            await self._show(
                interaction,
                login_failed_card(
                    server_url,
                    "the Omnigent server doesn't support Device Authorization Grant. "
                    "Please contact your Omnigent server administrator.",
                ),
            )
            return
        except OAuthError as exc:
            self._logger.info("Login authorize failed server=%s error=%s", server_url, exc)
            await self._show(
                interaction,
                login_failed_card(server_url, "could not start login. Try again shortly."),
            )
            return

        # Show the "open the link and approve" screen. If this fails (the
        # interaction expired, the user dismissed it) before the background poll
        # is spawned, close ``pending`` — otherwise its open httpx client leaks,
        # since nothing else owns it until the poll takes over below.
        try:
            await interaction.edit_original_response(
                embed=to_embed(
                    login_card(server_url, pending.verification_url, pending.user_code)
                ),
                view=_LinkView("Sign in to Omnigent", pending.verification_url),
            )
        except Exception:
            await pending.close()
            raise

        async def _on_success() -> None:
            await self._revalidate_and_advance(interaction, user_id=user_id, server_url=server_url)

        async def _on_failure(reason: str) -> None:
            await self._show(interaction, login_failed_card(server_url, reason))

        self._auth.await_authorization_in_background(
            pending=pending,
            user_id=user_id,
            server_url=server_url,
            on_success=_on_success,
            on_failure=_on_failure,
        )

    async def _revalidate_and_advance(
        self, interaction: Any, *, user_id: str, server_url: str
    ) -> None:
        """Post-login: re-validate as the user, then show the selects.

        Drops the tokenless client pooled during the pre-login probe so the pool
        rebuilds it with the freshly-stored token — otherwise ``validate``
        re-hits the auth wall and setup stalls.
        """
        await self._pool.invalidate(server_url, user_id)
        omnigent = await self._pool.get(server_url, user_id)
        try:
            validated = await omnigent.validate()
            await self._advance_to_select(interaction, omnigent, user_id, validated)
        except Exception as exc:
            # The token was stored (login succeeded), but validating it against
            # the server failed — most often the granted scope doesn't satisfy
            # the server. Surface it rather than leaving the message on
            # "Waiting for approval…", and log with a traceback.
            self._logger.warning(
                "Post-login advance failed user=%s server=%s: %s",
                user_id,
                server_url,
                exc,
                exc_info=True,
            )
            await self._show(
                interaction,
                login_failed_card(
                    server_url,
                    "you're signed in, but the server rejected the sign-in when "
                    "validating it. Ask your Omnigent operator to confirm the login "
                    "is accepted by the server.",
                ),
            )

    async def save_config(self, interaction: Any, user_id: str, config: UserConfig) -> None:
        """Persist a completed setup and confirm it (modal-submit entry point)."""
        await self._store.upsert_user_config(user_id, config)
        self._logger.info(
            "Saved Omnigent setup user=%s server=%s agent=%s host=%s",
            user_id,
            self._server_url,
            config.agent_id,
            config.host_id,
        )
        await interaction.response.send_message(
            embed=to_embed(saved_card(config, self._server_url)), ephemeral=True
        )

    async def run_logout(self, interaction: Any) -> None:
        """Handle ``/omnigent logout`` — full reset for the user.

        Revokes every delegated token the user holds and clears all their saved
        settings (agent/host/workspace plus channel→session mappings).
        """
        user_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        revoked = 0
        if self._auth is not None and self._auth.enabled:
            revoked = await self._auth.logout_all(user_id)
            # Drop any pooled clients holding the just-revoked tokens.
            await self._pool.invalidate_user(user_id)
        await self._store.clear_user_data(user_id)
        servers = f" and revoked {revoked} server login(s)" if revoked else ""
        await interaction.edit_original_response(
            content=(
                f"👋 Logged out{servers}. Your Omnigent settings were cleared — "
                "run `/omnigent config` to set up again."
            )
        )

    async def _resolve_default_workspace(
        self, client: OmnigentClient, online_hosts: list[dict[str, Any]]
    ) -> str:
        for host in online_hosts:
            host_id = host_id_of(host)
            if host_id is None:
                continue
            try:
                home = await client.get_host_home(host_id)
            except OmnigentError as exc:
                self._logger.info("Could not resolve host home host_id=%s error=%s", host_id, exc)
                home = None
            if home:
                return home
        return default_workspace()

    async def _show(self, interaction: Any, card: Card) -> None:
        """Replace the ephemeral setup message with ``card`` (best-effort).

        Called from background login tasks as well as the command path, where
        the interaction token may have expired or the user dismissed the
        message — a failure there must never raise into a fire-and-forget task.
        """
        try:
            await interaction.edit_original_response(embed=to_embed(card), view=None)
        except Exception as exc:
            self._logger.info("Setup message update failed: %s", exc)
