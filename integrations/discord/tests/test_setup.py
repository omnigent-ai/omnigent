"""The ``/omnigent`` setup flow and the copy it shows.

The flow is one ephemeral message the bot edits in place: connecting → (login) →
agent/host selects → workspace modal → saved. The content builders are pure, so
the copy is asserted directly; the flow is driven with a fake interaction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from omnigent_bot_core.oauth import DeviceGrantUnavailableError, OAuthError, PendingLogin
from omnigent_bot_core.omnigent import (
    AuthRequiredError,
    OmnigentError,
    ServerUnreachableError,
    ValidatedServer,
)
from omnigent_discord.models import UserConfig
from omnigent_discord.setup import (
    SetupFlow,
    agent_options,
    default_workspace,
    host_options,
    host_unavailable_text,
    relogin_required_text,
    saved_card,
    setup_required_text,
)
from omnigent_discord.store import SQLiteStore
from omnigent_discord.text import MAX_SELECT_OPTIONS

SERVER = "https://omnigent.example.com"
LOGGER = logging.getLogger("test")
AGENTS = [{"id": "ag_1", "name": "debby"}]
HOSTS = [{"host_id": "h1", "name": "Host One", "status": "online"}]


# ── fakes ─────────────────────────────────────────────────────────────────


class FakeUser:
    def __init__(self, user_id: str = "1001") -> None:
        self.id = int(user_id)


class FakeGuild:
    def __init__(self, name: str = "Acme Guild") -> None:
        self.id = 900
        self.name = name


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.sent: list[dict[str, Any]] = []
        self.modals: list[Any] = []

    async def defer(self, **_kwargs: Any) -> None:
        self.deferred = True

    async def send_message(self, content: str | None = None, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})

    async def send_modal(self, modal: Any) -> None:
        self.modals.append(modal)


class FakeInteraction:
    """Records every edit of the ephemeral setup message."""

    def __init__(self, user_id: str = "1001", guild: FakeGuild | None = None) -> None:
        self.user = FakeUser(user_id)
        self.guild = guild
        self.response = FakeResponse()
        self.edits: list[dict[str, Any]] = []

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)

    # ── helpers ──────────────────────────────────────────────────────────
    @property
    def last_embed(self) -> Any:
        return next(e["embed"] for e in reversed(self.edits) if e.get("embed") is not None)

    @property
    def last_view(self) -> Any:
        return next((e.get("view") for e in reversed(self.edits) if e.get("view")), None)

    def shows(self, needle: str) -> bool:
        embed = self.last_embed
        return needle in (embed.description or "") or needle in (embed.title or "")


class FakeSetupClient:
    def __init__(
        self,
        *,
        agents: list[dict[str, Any]] | None = None,
        hosts: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        host_home: str | None = "/home/bot",
    ) -> None:
        self.agents = AGENTS if agents is None else agents
        self.hosts = HOSTS if hosts is None else hosts
        self.error = error
        self.host_home = host_home

    async def validate(self) -> ValidatedServer:
        if self.error is not None:
            raise self.error
        return ValidatedServer(agents=self.agents, online_hosts=self.hosts)

    async def get_host_home(self, host_id: str) -> str | None:
        return self.host_home


class FakePool:
    def __init__(self, client: FakeSetupClient) -> None:
        self.client = client
        self.invalidated: list[str] = []

    async def get(self, server_url: str, user_id: str = "") -> FakeSetupClient:
        return self.client

    async def invalidate(self, server_url: str, user_id: str) -> None:
        self.invalidated.append(user_id)

    async def invalidate_user(self, user_id: str) -> None:
        self.invalidated.append(user_id)


class FakeAuth:
    """A stand-in for :class:`AuthManager` covering only what setup calls."""

    def __init__(self, *, enabled: bool = True, authorize_error: Exception | None = None):
        self.enabled = enabled
        self.authorize_error = authorize_error
        self.closed = False
        self.revoked = 0
        self.on_success: Any = None
        self.on_failure: Any = None

    async def authorize(self, *, server_url: str, client_id: str) -> PendingLogin:
        self.client_id = client_id
        if self.authorize_error is not None:
            raise self.authorize_error

        async def _poll() -> Any:  # pragma: no cover - never polled in these tests
            raise AssertionError("poll should be driven by the caller")

        async def _close() -> None:
            self.closed = True

        return PendingLogin(
            verification_url=f"{SERVER}/oauth/device?user_code=ABCD",
            user_code="ABCD",
            _poll=_poll,
            _close=_close,
        )

    def await_authorization_in_background(self, **kwargs: Any) -> None:
        self.on_success = kwargs["on_success"]
        self.on_failure = kwargs["on_failure"]

    async def logout_all(self, user_id: str) -> int:
        return self.revoked


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "bot.sqlite3")
    await store.initialize()
    return store


def _flow(store: SQLiteStore, client: FakeSetupClient, auth: FakeAuth | None = None) -> SetupFlow:
    return SetupFlow(
        store=store,
        pool=FakePool(client),  # type: ignore[arg-type]
        server_url=SERVER,
        auth_manager=auth,  # type: ignore[arg-type]
    )


# ── pure content ──────────────────────────────────────────────────────────


def test_agent_options_pair_the_name_with_the_id() -> None:
    assert agent_options(AGENTS) == [("debby", "ag_1")]


def test_agent_without_an_id_is_skipped() -> None:
    assert agent_options([{"name": "broken"}]) == []


def test_agent_options_fit_discords_option_cap() -> None:
    many = [{"id": f"ag_{i}", "name": f"agent {i}"} for i in range(100)]
    assert len(agent_options(many)) == MAX_SELECT_OPTIONS


def test_host_options_read_either_id_key() -> None:
    assert host_options([{"id": "h9", "name": "Nine"}]) == [("Nine", "h9")]
    assert host_options(HOSTS) == [("Host One", "h1")]


def test_host_without_a_name_falls_back_to_its_id() -> None:
    assert host_options([{"host_id": "h1"}]) == [("h1", "h1")]


def test_no_host_guidance_names_the_command_and_the_server() -> None:
    text = host_unavailable_text(SERVER)
    assert f"omni host --server {SERVER}" in text
    assert "/omnigent config" in text


def test_setup_and_relogin_nudges_name_the_command() -> None:
    assert "/omnigent config" in setup_required_text()
    # This nudge often lands in a DM, where the command may not be available —
    # so it has to say where to run it.
    assert "server channel" in setup_required_text()
    assert "/omnigent config" in relogin_required_text()


def test_saved_card_repeats_every_choice_back() -> None:
    card = saved_card(
        UserConfig("ag_1", "debby", "/srv/work", host_id="h1", host_name="Host One"), SERVER
    )
    assert "debby" in card.description
    assert "Host One" in card.description
    assert "/srv/work" in card.description


def test_workspace_fallback_is_an_absolute_path() -> None:
    assert default_workspace().startswith("/")


# ── the happy path ────────────────────────────────────────────────────────


async def test_config_offers_the_agent_and_host_selects(store: SQLiteStore) -> None:
    interaction = FakeInteraction()
    await _flow(store, FakeSetupClient()).run_config(interaction)
    assert interaction.response.deferred is True  # the probe can take a moment
    assert interaction.shows(SERVER)
    view = interaction.last_view
    placeholders = [getattr(item, "placeholder", None) for item in view.children]
    assert "Choose an agent" in placeholders
    assert "Choose a host" in placeholders


async def test_workspace_defaults_to_the_hosts_home(store: SQLiteStore) -> None:
    # Runners start on the host, not wherever the bot process happens to run.
    interaction = FakeInteraction()
    await _flow(store, FakeSetupClient(host_home="/home/runner")).run_config(interaction)
    save_button = interaction.last_view.children[-1]
    assert save_button.label == "Save workspace"
    assert interaction.last_view._workspace_default == "/home/runner"


async def test_unprobeable_host_falls_back_to_the_bots_cwd(store: SQLiteStore) -> None:
    interaction = FakeInteraction()
    await _flow(store, FakeSetupClient(host_home=None)).run_config(interaction)
    assert interaction.last_view._workspace_default == default_workspace()


async def test_saving_persists_the_choice_and_confirms_it(store: SQLiteStore) -> None:
    interaction = FakeInteraction()
    config = UserConfig("ag_1", "debby", "/srv/work", host_id="h1", host_name="Host One")
    await _flow(store, FakeSetupClient()).save_config(interaction, "1001", config)
    assert await store.get_user_config("1001") == config
    assert interaction.response.sent[0]["ephemeral"] is True


# ── setup that cannot finish ──────────────────────────────────────────────


async def test_server_with_no_agents_says_so(store: SQLiteStore) -> None:
    interaction = FakeInteraction()
    await _flow(store, FakeSetupClient(agents=[])).run_config(interaction)
    assert interaction.shows("no agents available")
    assert interaction.last_view is None


async def test_no_online_host_shows_how_to_start_one(store: SQLiteStore) -> None:
    # A session needs a host to run on, so setup can't finish without one.
    interaction = FakeInteraction()
    await _flow(store, FakeSetupClient(hosts=[])).run_config(interaction)
    assert interaction.shows(f"omni host --server {SERVER}")


async def test_unreachable_server_says_to_check_the_url_and_port(
    store: SQLiteStore,
) -> None:
    interaction = FakeInteraction()
    client = FakeSetupClient(error=ServerUnreachableError("connection refused"))
    await _flow(store, client).run_config(interaction)
    assert interaction.shows("couldn't be reached")
    assert interaction.shows("OMNIGENT_SERVER_URL")
    # A raw transport error can name internal hosts; it stays in the log.
    assert not interaction.shows("connection refused")


async def test_server_that_answers_with_an_error_is_not_called_unreachable(
    store: SQLiteStore,
) -> None:
    # These send the operator to completely different places. A stale server
    # holding the expected port answers 5xx; calling that "unreachable" makes a
    # running server look like a down one and hides the real cause.
    interaction = FakeInteraction()
    client = FakeSetupClient(error=OmnigentError("status 500"))
    await _flow(store, client).run_config(interaction)
    assert interaction.shows("answered, but with an error")
    assert not interaction.shows("couldn't be reached")
    # The raw status stays in the log, not the card.
    assert not interaction.shows("status 500")


async def test_neither_server_failure_blames_a_login_that_never_happened(
    store: SQLiteStore,
) -> None:
    for error in (ServerUnreachableError("down"), OmnigentError("500")):
        interaction = FakeInteraction()
        await _flow(store, FakeSetupClient(error=error)).run_config(interaction)
        assert not interaction.shows("Login")


async def test_server_needing_login_without_auth_wired_says_who_to_ask(
    store: SQLiteStore,
) -> None:
    interaction = FakeInteraction()
    client = FakeSetupClient(error=AuthRequiredError("401"))
    await _flow(store, client, FakeAuth(enabled=False)).run_config(interaction)
    assert interaction.shows("Ask the bot operator")


# ── login ─────────────────────────────────────────────────────────────────


async def test_server_needing_login_shows_a_one_click_link(store: SQLiteStore) -> None:
    interaction = FakeInteraction(guild=FakeGuild())
    auth = FakeAuth()
    client = FakeSetupClient(error=AuthRequiredError("401"))
    await _flow(store, client, auth).run_config(interaction)
    assert interaction.shows("Open the login page")
    # The short code lets the user confirm the consent page is the same request.
    assert interaction.shows("ABCD")
    link_button = interaction.last_view.children[0]
    assert link_button.url.startswith(SERVER)


async def test_login_names_the_guild_so_the_grant_is_attributable(
    store: SQLiteStore,
) -> None:
    interaction = FakeInteraction(guild=FakeGuild("Acme Guild"))
    auth = FakeAuth()
    await _flow(store, FakeSetupClient(error=AuthRequiredError("401")), auth).run_config(
        interaction
    )
    assert auth.client_id == "Discord-Omnigent-Acme Guild"


async def test_login_from_a_dm_still_identifies_the_integration(
    store: SQLiteStore,
) -> None:
    interaction = FakeInteraction(guild=None)
    auth = FakeAuth()
    await _flow(store, FakeSetupClient(error=AuthRequiredError("401")), auth).run_config(
        interaction
    )
    assert auth.client_id == "Discord-Omnigent"


async def test_completed_login_advances_to_the_selects(store: SQLiteStore) -> None:
    interaction = FakeInteraction()
    auth = FakeAuth()
    client = FakeSetupClient(error=AuthRequiredError("401"))
    flow = _flow(store, client, auth)
    await flow.run_config(interaction)
    # The token is now stored, so the next validate succeeds.
    client.error = None
    await auth.on_success()
    assert interaction.last_view is not None
    assert interaction.shows(SERVER)


async def test_pooled_tokenless_client_is_dropped_after_login(
    store: SQLiteStore,
) -> None:
    # Reusing the pre-login client would re-hit the auth wall and stall setup.
    interaction = FakeInteraction()
    auth = FakeAuth()
    client = FakeSetupClient(error=AuthRequiredError("401"))
    pool = FakePool(client)
    flow = SetupFlow(store=store, pool=pool, server_url=SERVER, auth_manager=auth)  # type: ignore[arg-type]
    await flow.run_config(interaction)
    client.error = None
    await auth.on_success()
    assert pool.invalidated == ["1001"]


async def test_login_the_server_then_rejects_is_surfaced(store: SQLiteStore) -> None:
    # The browser shows success while the modal would otherwise hang forever.
    interaction = FakeInteraction()
    auth = FakeAuth()
    client = FakeSetupClient(error=AuthRequiredError("401"))
    await _flow(store, client, auth).run_config(interaction)
    await auth.on_success()  # validate still 401s
    assert interaction.shows("rejected the sign-in")


async def test_denied_login_reports_the_reason(store: SQLiteStore) -> None:
    interaction = FakeInteraction()
    auth = FakeAuth()
    await _flow(store, FakeSetupClient(error=AuthRequiredError("401")), auth).run_config(
        interaction
    )
    await auth.on_failure("You denied the login request.")
    assert interaction.shows("You denied the login request.")


async def test_server_without_the_device_grant_names_the_admin_fix(
    store: SQLiteStore,
) -> None:
    interaction = FakeInteraction()
    auth = FakeAuth(authorize_error=DeviceGrantUnavailableError("404"))
    await _flow(store, FakeSetupClient(error=AuthRequiredError("401")), auth).run_config(
        interaction
    )
    assert interaction.shows("Device Authorization Grant")


async def test_login_that_cannot_start_says_to_retry(store: SQLiteStore) -> None:
    interaction = FakeInteraction()
    auth = FakeAuth(authorize_error=OAuthError("boom"))
    await _flow(store, FakeSetupClient(error=AuthRequiredError("401")), auth).run_config(
        interaction
    )
    assert interaction.shows("could not start login")


# ── logout ────────────────────────────────────────────────────────────────


async def test_logout_revokes_and_clears_everything(store: SQLiteStore) -> None:
    await store.upsert_user_config("1001", UserConfig("ag_1", "debby", "/w"))
    interaction = FakeInteraction()
    auth = FakeAuth()
    auth.revoked = 2
    await _flow(store, FakeSetupClient(), auth).run_logout(interaction)
    assert await store.get_user_config("1001") is None
    assert "revoked 2 server login(s)" in interaction.edits[-1]["content"]


async def test_logout_without_a_login_still_clears_settings(store: SQLiteStore) -> None:
    await store.upsert_user_config("1001", UserConfig("ag_1", "debby", "/w"))
    interaction = FakeInteraction()
    await _flow(store, FakeSetupClient(), FakeAuth()).run_logout(interaction)
    assert await store.get_user_config("1001") is None
    assert "revoked" not in interaction.edits[-1]["content"]
