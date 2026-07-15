from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from slack_bolt.async_app import AsyncApp

from omnigent_slack.auth_manager import AuthManager, pack_user_key, slack_client_id
from omnigent_slack.models import UserConfig
from omnigent_slack.oauth import OAuthError
from omnigent_slack.omnigent import (
    AuthRequiredError,
    OmnigentClient,
    OmnigentClientPool,
    OmnigentError,
    ValidatedServer,
)
from omnigent_slack.store import SQLiteStore

# Block Kit identifiers shared by the modal builders and the submission
# handlers. Keeping them in one place avoids drift between what a modal renders
# and what its handler reads back out of the ``view.state`` payload.
ACTION_SETUP_START = "omnigent_setup_start"
CALLBACK_URL_MODAL = "omnigent_setup_url"
CALLBACK_SELECT_MODAL = "omnigent_setup_select"

# Slash command that lets a user (re)configure their Omnigent server.
COMMAND_NAME = "/omnigent"

URL_BLOCK = "server_url_block"
URL_ACTION = "server_url_input"
AGENT_BLOCK = "agent_block"
AGENT_ACTION = "agent_select"
HOST_BLOCK = "host_block"
HOST_ACTION = "host_select"
WORKSPACE_BLOCK = "workspace_block"
WORKSPACE_ACTION = "workspace_input"

# Slack caps a static_select at 100 options; both agents and hosts are far
# below that in practice, but truncate defensively so a huge server never
# produces an invalid view payload.
_MAX_SELECT_OPTIONS = 100


class _ViewUpdateAck:
    """Adapts ``views_update`` to the ``ack(response_action=...)`` shape.

    The modal builders and :meth:`SetupFlow._advance_to_select` speak the
    view-submission ``ack`` protocol (``response_action='update'|'errors'``
    + ``view``/``errors``). After login the modal is advanced from a
    background task where there is no live ``ack`` — only the modal's
    ``view_id`` — so this shim turns the same call into a ``views_update``.
    """

    def __init__(self, client: Any, view_id: str) -> None:
        self._client = client
        self._view_id = view_id

    async def __call__(self, **kwargs: Any) -> None:
        view = kwargs.get("view")
        if view is not None:
            await self._client.views_update(view_id=self._view_id, view=view)
        # An 'errors' response_action has no meaning outside a live
        # submission; surface it as a simple failure screen instead.
        elif kwargs.get("response_action") == "errors":
            errors = kwargs.get("errors") or {}
            reason = next(iter(errors.values()), "Setup could not continue.")
            await self._client.views_update(
                view_id=self._view_id, view=login_failed_modal("", str(reason))
            )


class SetupFlow:
    """Per-user Omnigent setup: a DM button opens a two-step modal.

    Step one asks for the server URL and validates connectivity; step two lets
    the user pick an agent and preferred host from menus populated by that
    server. The result is persisted per ``(team_id, user_id)``.
    """

    def __init__(
        self,
        store: SQLiteStore,
        pool: OmnigentClientPool,
        auth_manager: AuthManager | None = None,
    ) -> None:
        self._store = store
        self._pool = pool
        self._auth = auth_manager
        self._logger = logging.getLogger(__name__)

    def register(self, app: AsyncApp) -> None:
        app.command(COMMAND_NAME)(self._handle_config_command)
        app.action(ACTION_SETUP_START)(self._handle_setup_start)
        app.view(CALLBACK_URL_MODAL)(self._handle_url_submit)
        app.view(CALLBACK_SELECT_MODAL)(self._handle_select_submit)

    async def _handle_config_command(self, ack: Any, command: dict[str, Any], client: Any) -> None:
        # Two commands: ``/omnigent`` (or ``/omnigent config``) opens the setup
        # modal — login is folded into that flow, an auth-enabled server shows
        # the login link in the modal and advances once approved. ``/omnigent
        # logout`` revokes every server token and clears all saved settings for
        # the user. Any other argument opens setup. Prefill the setup URL with
        # the current config so a reconfigure starts from what they have.
        await ack()
        team_id = str(command.get("team_id") or "")
        user_id = str(command.get("user_id") or "")
        subcommand = str(command.get("text") or "").split()[:1]

        if subcommand and subcommand[0].lower() == "logout":
            await self._handle_logout(team_id=team_id, user_id=user_id, client=client)
            return

        trigger_id = command.get("trigger_id")
        if not trigger_id:
            self._logger.warning("Config command missing trigger_id")
            return
        existing = await self._store.get_user_config(team_id, user_id)
        current_url = existing.server_url if existing else None
        await client.views_open(trigger_id=trigger_id, view=url_modal(server_url=current_url))

    async def _handle_logout(self, *, team_id: str, user_id: str, client: Any) -> None:
        """Handle ``/omnigent logout`` — full reset for the user.

        Revokes every delegated token the user holds (across all servers)
        and clears all their saved settings (server/agent/host/workspace
        plus thread→session mappings), then DMs a confirmation.
        """
        opened = await client.conversations_open(users=user_id)
        dm_channel = _dm_channel_id(opened)
        revoked = 0
        if self._auth is not None and self._auth.enabled:
            revoked = await self._auth.logout_all(team_id, user_id)
            # Drop any pooled clients holding the just-revoked tokens.
            await self._pool.invalidate_user(pack_user_key(team_id, user_id))
        await self._store.clear_user_data(team_id, user_id)
        if dm_channel:
            servers = f" and revoked {revoked} server login(s)" if revoked else ""
            await client.chat_postMessage(
                channel=dm_channel,
                text=(
                    f":wave: Logged out{servers}. Your Omnigent settings were "
                    "cleared — run `/omnigent` to set up again."
                ),
            )
        else:
            self._logger.warning("Could not open DM to confirm logout user=%s", user_id)

    async def prompt_unconfigured(
        self,
        client: Any,
        user_id: str,
        *,
        channel: str,
        thread_ts: str | None,
        in_channel: bool,
    ) -> None:
        """Nudge an unconfigured user into the DM setup flow.

        Always DMs the user the setup button. When the trigger came from a
        channel, also drops an ephemeral pointer in the thread so the user
        knows to check their DM rather than waiting for a reply that never
        comes.
        """
        opened = await client.conversations_open(users=user_id)
        dm_channel = _dm_channel_id(opened)
        if dm_channel:
            await client.chat_postMessage(
                channel=dm_channel,
                text="Set up Omnigent to start using me.",
                blocks=setup_prompt_blocks(),
            )
        else:
            self._logger.warning("Could not open DM for setup user=%s", user_id)

        if in_channel:
            await client.chat_postEphemeral(
                channel=channel,
                user=user_id,
                thread_ts=thread_ts,
                text="Let's get you set up — check your DM with me to configure Omnigent.",
            )

    async def _handle_setup_start(self, ack: Any, body: dict[str, Any], client: Any) -> None:
        await ack()
        trigger_id = body.get("trigger_id")
        if not trigger_id:
            self._logger.warning("Setup start action missing trigger_id")
            return
        await client.views_open(trigger_id=trigger_id, view=url_modal())

    async def _handle_url_submit(
        self, ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        server_url = _input_value(view, URL_BLOCK, URL_ACTION).strip()
        if not server_url:
            await ack(
                response_action="errors",
                errors={URL_BLOCK: "Enter your Omnigent server URL."},
            )
            return
        if not server_url.startswith(("http://", "https://")):
            await ack(
                response_action="errors",
                errors={URL_BLOCK: "URL must start with http:// or https://."},
            )
            return

        team_id = str((body.get("team") or {}).get("id") or body.get("team_id") or "")
        user_id = str((body.get("user") or {}).get("id") or "")
        view_id = str((body.get("view") or {}).get("id") or "")
        # Validate as the authenticated user when a token exists — the
        # agent/host listing endpoints are auth-gated.
        omnigent = await self._pool.get(server_url, pack_user_key(team_id, user_id))
        try:
            validated = await omnigent.validate()
        except AuthRequiredError:
            # The server needs auth and this user hasn't logged in yet. Login
            # happens inside this same modal: show the verification link, poll
            # in the background, and advance the modal to agent/host selection
            # the moment the user approves — no DM, no re-running /omnigent.
            if self._auth is None or not self._auth.enabled:
                await ack(
                    response_action="errors",
                    errors={
                        URL_BLOCK: (
                            "This server requires login, which this bot isn't "
                            "configured for. Ask the bot operator to enable it."
                        )
                    },
                )
                return
            await self._begin_in_modal_login(
                ack,
                client,
                team_id=team_id,
                user_id=user_id,
                server_url=server_url,
                view_id=view_id,
            )
            return
        except OmnigentError as exc:
            self._logger.info("Setup validation failed url=%s error=%s", server_url, exc)
            await ack(
                response_action="errors",
                errors={URL_BLOCK: "Could not reach that Omnigent server. Check the URL."},
            )
            return

        await self._advance_to_select(ack, omnigent, server_url, validated)

    async def _advance_to_select(
        self,
        ack: Any,
        omnigent: OmnigentClient,
        server_url: str,
        validated: ValidatedServer,
    ) -> None:
        """Move the modal from URL entry to agent/host/workspace selection.

        ``ack`` may be a real Slack ``ack`` (initial submit) or an
        :class:`_ViewUpdateAck` that pushes a ``views_update`` for the
        post-login continuation — both take ``response_action='update'``.
        """
        if not validated.agents:
            await ack(
                response_action="errors",
                errors={URL_BLOCK: "That server has no agents available."},
            )
            return
        if not validated.online_hosts:
            # A session needs a host to run on, so setup can't finish without
            # one. Swap the modal for the same guidance a turn shows when no
            # host is reachable, telling the user how to bring one online.
            await ack(response_action="update", view=no_host_modal(server_url))
            return
        # Default the workspace to the host's home directory (where runners
        # actually run), not the bot process's cwd. Fall back to the bot's cwd
        # only if the host can't be probed.
        workspace_default = await self._resolve_default_workspace(omnigent, validated.online_hosts)
        await ack(
            response_action="update",
            view=select_modal(server_url, validated, workspace_default=workspace_default),
        )

    async def _begin_in_modal_login(
        self,
        ack: Any,
        client: Any,
        *,
        team_id: str,
        user_id: str,
        server_url: str,
        view_id: str,
    ) -> None:
        """Show the login link in the modal and advance it once approved."""
        assert self._auth is not None
        client_id = slack_client_id(await self._team_name(client, team_id))
        try:
            pending = await self._auth.authorize(server_url=server_url, client_id=client_id)
        except OAuthError as exc:
            self._logger.info("Login authorize failed server=%s error=%s", server_url, exc)
            await ack(
                response_action="errors",
                errors={URL_BLOCK: f"Could not start login on {server_url}. Check the URL."},
            )
            return

        # Swap the modal to the "open the link and approve" screen.
        await ack(
            response_action="update",
            view=login_waiting_modal(server_url, pending.verification_url, pending.user_code),
        )

        async def _on_success() -> None:
            # Re-validate as the now-authenticated user and advance the same
            # modal to the agent/host picker via views_update. A views_update
            # can fail if the user already closed the modal — log, don't crash
            # the background task (the token is stored regardless).
            omnigent = await self._pool.get(server_url, pack_user_key(team_id, user_id))
            try:
                validated = await omnigent.validate()
                await self._advance_to_select(
                    _ViewUpdateAck(client, view_id), omnigent, server_url, validated
                )
            except Exception as exc:
                self._logger.info("Post-login modal advance failed: %s", exc)

        async def _on_failure(reason: str) -> None:
            try:
                await client.views_update(
                    view_id=view_id, view=login_failed_modal(server_url, reason)
                )
            except Exception as exc:
                self._logger.info("Login-failure modal update failed: %s", exc)

        self._auth.await_authorization_in_background(
            pending=pending,
            team_id=team_id,
            user_id=user_id,
            server_url=server_url,
            on_success=_on_success,
            on_failure=_on_failure,
        )

    async def _team_name(self, client: Any, team_id: str) -> str:
        """Resolve the Slack workspace's display name via ``team.info``.

        Used only to label the ``client_id`` sent to the Omnigent server.
        Best-effort: any API failure (missing ``team:read`` scope, network)
        falls back to an empty string, so login still proceeds with the
        bare ``Slack-Omnigent`` client id.
        """
        try:
            resp = await client.team_info(team=team_id)
        except Exception as exc:  # noqa: BLE001 — label lookup must never block login
            self._logger.info("team.info lookup failed team=%s error=%s", team_id, exc)
            return ""
        team = resp.get("team") if hasattr(resp, "get") else None
        return str(team.get("name") or "") if isinstance(team, dict) else ""

    async def _resolve_default_workspace(
        self, client: OmnigentClient, online_hosts: list[dict[str, Any]]
    ) -> str:
        for host in online_hosts:
            host_id = host.get("host_id") or host.get("id")
            if not isinstance(host_id, str):
                continue
            try:
                home = await client.get_host_home(host_id)
            except OmnigentError as exc:
                self._logger.info("Could not resolve host home host_id=%s error=%s", host_id, exc)
                home = None
            if home:
                return home
        return default_workspace()

    async def _handle_select_submit(
        self, ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        server_url = str(view.get("private_metadata") or "")
        agent_option = _selected_option(view, AGENT_BLOCK, AGENT_ACTION)
        if not server_url or agent_option is None:
            await ack(
                response_action="errors",
                errors={AGENT_BLOCK: "Select an agent to finish setup."},
            )
            return

        workspace = _input_value(view, WORKSPACE_BLOCK, WORKSPACE_ACTION).strip()
        if not workspace.startswith("/"):
            await ack(
                response_action="errors",
                errors={WORKSPACE_BLOCK: "Enter an absolute workspace path (starting with /)."},
            )
            return

        host_option = _selected_option(view, HOST_BLOCK, HOST_ACTION)
        if host_option is None:
            await ack(
                response_action="errors",
                errors={HOST_BLOCK: "Select a host to run your sessions on."},
            )
            return
        host_id = str(host_option.get("value"))
        host_name = _option_text(host_option)

        config = UserConfig(
            server_url=server_url,
            agent_id=str(agent_option.get("value")),
            agent_name=_option_text(agent_option) or str(agent_option.get("value")),
            workspace=workspace,
            host_id=host_id,
            host_name=host_name,
        )

        team_id = str((body.get("team") or {}).get("id") or body.get("team_id") or "")
        user_id = str((body.get("user") or {}).get("id") or "")
        await self._store.upsert_user_config(team_id, user_id, config)
        await ack()
        self._logger.info(
            "Saved Omnigent setup team=%s user=%s server=%s agent=%s host=%s",
            team_id,
            user_id,
            server_url,
            config.agent_id,
            host_id,
        )

        opened = await client.conversations_open(users=user_id)
        dm_channel = _dm_channel_id(opened)
        if dm_channel:
            host_line = f" on host *{host_name}*" if host_name else ""
            await client.chat_postMessage(
                channel=dm_channel,
                text=(
                    f":white_check_mark: You're set up! I'll use *{config.agent_name}*"
                    f"{host_line} on {server_url}. Mention me in a channel or message me "
                    "here to start."
                ),
            )


def default_workspace() -> str:
    # Fallback default when the host's home directory can't be resolved. Only
    # meaningful when the bot and host share a machine; the user can override it.
    return str(Path.cwd())


def host_unavailable_text(server_url: str) -> str:
    # Shown both during setup (no online host to pick) and at turn time (the
    # chosen host went offline). Single source of truth so the guidance stays
    # identical everywhere.
    return (
        ":warning: No online host is available to run your session.\n"
        "Run this on the machine you want to use, then run /omnigent:\n"
        f"`omni host --server {server_url}`"
    )


def setup_prompt_blocks() -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Set up Omnigent*\nPoint me at your Omnigent server so I can run "
                    "sessions for you."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⚙️ Set up Omnigent"},
                    "style": "primary",
                    "action_id": ACTION_SETUP_START,
                }
            ],
        },
    ]


def url_modal(server_url: str | None = None) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": URL_ACTION,
        "placeholder": {"type": "plain_text", "text": "https://omnigent.example.com"},
    }
    if server_url:
        element["initial_value"] = server_url
    return {
        "type": "modal",
        "callback_id": CALLBACK_URL_MODAL,
        "title": {"type": "plain_text", "text": "Set up Omnigent"},
        "submit": {"type": "plain_text", "text": "Connect"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": URL_BLOCK,
                "label": {"type": "plain_text", "text": "Omnigent server URL"},
                "element": element,
            }
        ],
    }


def no_host_modal(server_url: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": CALLBACK_URL_MODAL,
        "title": {"type": "plain_text", "text": "Set up Omnigent"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": host_unavailable_text(server_url)},
            }
        ],
    }


def login_waiting_modal(server_url: str, verification_url: str, user_code: str) -> dict[str, Any]:
    # Shown in-modal when setup hits an auth-enabled server. The user opens
    # the link and approves in their browser; the modal then advances itself
    # to the agent/host picker via views_update — no DM, no re-running the
    # command. No submit button: this screen just waits.
    # Device-grant flows show a short user_code to match on the consent page;
    # the OIDC ticket flow has none (the IdP page needs no code).
    code_hint = f" (code `{user_code}`)" if user_code else ""
    return {
        "type": "modal",
        "callback_id": CALLBACK_URL_MODAL,
        "title": {"type": "plain_text", "text": "Set up Omnigent"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{server_url}* requires login.\n\n"
                        f"1. <{verification_url}|Open the login page> and sign in"
                        f"{code_hint}.\n"
                        "2. This window will continue automatically once you're done."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "Waiting for approval… keep this window open."}
                ],
            },
        ],
    }


def login_failed_modal(server_url: str, reason: str) -> dict[str, Any]:
    # Terminal screen when login is denied, expires, or errors. The user
    # re-runs /omnigent to try again.
    where = f" to *{server_url}*" if server_url else ""
    return {
        "type": "modal",
        "callback_id": CALLBACK_URL_MODAL,
        "title": {"type": "plain_text", "text": "Set up Omnigent"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":warning: Login{where} didn't complete: {reason}\n"
                        "Run `/omnigent` to try again."
                    ),
                },
            }
        ],
    }


def select_modal(
    server_url: str,
    validated: ValidatedServer,
    workspace_default: str | None = None,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"Connected to *{server_url}*."},
        },
        {
            "type": "input",
            "block_id": AGENT_BLOCK,
            "label": {"type": "plain_text", "text": "Agent"},
            "element": {
                "type": "static_select",
                "action_id": AGENT_ACTION,
                "placeholder": {"type": "plain_text", "text": "Choose an agent"},
                "options": _agent_options(validated.agents),
            },
        },
    ]
    host_options = _host_options(validated.online_hosts)
    blocks.append(
        {
            "type": "input",
            "block_id": HOST_BLOCK,
            "label": {"type": "plain_text", "text": "Host"},
            "element": {
                "type": "static_select",
                "action_id": HOST_ACTION,
                "placeholder": {"type": "plain_text", "text": "Choose a host"},
                "options": host_options,
            },
        }
    )
    blocks.append(
        {
            "type": "input",
            "block_id": WORKSPACE_BLOCK,
            "label": {"type": "plain_text", "text": "Workspace path"},
            "element": {
                "type": "plain_text_input",
                "action_id": WORKSPACE_ACTION,
                "initial_value": workspace_default or default_workspace(),
                "placeholder": {"type": "plain_text", "text": "/absolute/path/on/the/host"},
            },
            "hint": {
                "type": "plain_text",
                "text": "Absolute directory on the host where each session's runner starts.",
            },
        }
    )
    return {
        "type": "modal",
        "callback_id": CALLBACK_SELECT_MODAL,
        "private_metadata": server_url,
        "title": {"type": "plain_text", "text": "Set up Omnigent"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _agent_options(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for agent in agents[:_MAX_SELECT_OPTIONS]:
        agent_id = agent.get("id")
        name = agent.get("name") or agent_id
        if not isinstance(agent_id, str):
            continue
        options.append(_option(_plain(str(name)), agent_id))
    return options


def _host_options(hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for host in hosts[:_MAX_SELECT_OPTIONS]:
        host_id = host.get("host_id") or host.get("id")
        name = host.get("name") or host_id
        if not isinstance(host_id, str):
            continue
        options.append(_option(_plain(str(name)), host_id))
    return options


def _option(text: str, value: str) -> dict[str, Any]:
    return {"text": {"type": "plain_text", "text": text}, "value": value}


def _plain(text: str) -> str:
    # Slack option text is capped at 75 characters.
    return text if len(text) <= 75 else text[:74] + "…"


def _input_value(view: dict[str, Any], block_id: str, action_id: str) -> str:
    state = _state_action(view, block_id, action_id)
    value = state.get("value") if state else None
    return value if isinstance(value, str) else ""


def _selected_option(view: dict[str, Any], block_id: str, action_id: str) -> dict[str, Any] | None:
    state = _state_action(view, block_id, action_id)
    selected = state.get("selected_option") if state else None
    return selected if isinstance(selected, dict) else None


def _state_action(view: dict[str, Any], block_id: str, action_id: str) -> dict[str, Any] | None:
    values = view.get("state", {}).get("values", {})
    block = values.get(block_id)
    if not isinstance(block, dict):
        return None
    action = block.get(action_id)
    return action if isinstance(action, dict) else None


def _option_text(option: dict[str, Any]) -> str | None:
    text = option.get("text")
    if isinstance(text, dict):
        value = text.get("text")
        return value if isinstance(value, str) else None
    return None


def _dm_channel_id(opened: Any) -> str | None:
    # ``conversations_open`` returns a ``SlackResponse`` (async client), not a
    # plain dict — but it proxies ``.get``/``[]`` to the underlying payload, so
    # duck-type on ``.get`` rather than checking for ``dict``.
    get = getattr(opened, "get", None)
    if not callable(get):
        return None
    channel = get("channel")
    channel_get = getattr(channel, "get", None)
    if not callable(channel_get):
        return None
    channel_id = channel_get("id")
    return channel_id if isinstance(channel_id, str) else None
