from pathlib import Path
from typing import Any

import httpx
import respx
from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.setup import (
    AGENT_BLOCK,
    CALLBACK_SETUP_INFO,
    HOST_BLOCK,
    WORKSPACE_BLOCK,
    SetupFlow,
    connecting_modal,
    host_unavailable_text,
    no_agents_modal,
    no_host_modal,
    select_modal,
)
from omnigent_slack.store import SQLiteStore

_SERVER = "http://omnigent.test"


class FakeAck:
    """Captures the kwargs slack_bolt handlers pass to ack()."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FakeSetupClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.ephemeral: list[dict[str, Any]] = []
        self.opened_views: list[dict[str, Any]] = []
        self.updated_views: list[dict[str, Any]] = []

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        return {"channel": {"id": "D123"}}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1"}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.ephemeral.append(kwargs)
        return {"ok": True}

    async def views_open(self, **kwargs: Any) -> dict[str, Any]:
        self.opened_views.append(kwargs)
        # The real API returns the opened view (with its id) so setup can
        # drive it via views_update.
        return {"ok": True, "view": {"id": "V1"}}

    async def views_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated_views.append(kwargs)
        return {"ok": True}

    async def team_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "team": {"id": kwargs.get("team", "T1"), "name": "Acme Corp"}}


class SlackResponseLike:
    """Mimics slack_sdk's SlackResponse: not a dict, but proxies ``.get``/``[]``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class SlackResponseSetupClient(FakeSetupClient):
    """Like FakeSetupClient but returns a non-dict response from conversations_open."""

    async def conversations_open(self, **kwargs: Any) -> Any:
        return SlackResponseLike({"channel": SlackResponseLike({"id": "D123"})})


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    return store


def _flow(store: SQLiteStore, pool: OmnigentClientPool, auth: Any = None) -> SetupFlow:
    return SetupFlow(store=store, pool=pool, server_url=_SERVER, auth_manager=auth)


def test_select_modal_lists_agents_and_hosts() -> None:
    from omnigent_slack.omnigent import ValidatedServer

    view = select_modal(
        _SERVER,
        ValidatedServer(
            agents=[{"id": "ag_1", "name": "Helper"}],
            online_hosts=[{"host_id": "h1", "name": "Host One"}],
        ),
    )
    # The select modal shows the fixed server in its header text.
    assert any(_SERVER in str(b.get("text", {}).get("text", "")) for b in view["blocks"])
    blocks = {b["block_id"]: b for b in view["blocks"] if "block_id" in b}
    agent_opts = blocks[AGENT_BLOCK]["element"]["options"]
    assert [o["value"] for o in agent_opts] == ["ag_1"]
    host_opts = blocks[HOST_BLOCK]["element"]["options"]
    # Only real hosts are listed — the host is a required choice.
    assert [o["value"] for o in host_opts] == ["h1"]
    assert blocks[HOST_BLOCK].get("optional") is not True
    # A workspace input is present with a non-empty default.
    workspace_el = blocks[WORKSPACE_BLOCK]["element"]
    assert workspace_el["type"] == "plain_text_input"
    assert workspace_el["initial_value"]


def _last_update(client: FakeSetupClient) -> dict[str, Any]:
    assert client.updated_views, "expected a views_update"
    return client.updated_views[-1]["view"]


@respx.mock
async def test_setup_advances_to_select_modal_with_host_home_workspace(
    tmp_path: Path,
) -> None:
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/agents").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "ag_1", "name": "Helper"}]})
    )
    respx.get(_SERVER + "/v1/hosts").mock(
        return_value=httpx.Response(
            200, json={"hosts": [{"host_id": "h1", "name": "H", "status": "online"}]}
        )
    )
    respx.get(_SERVER + "/v1/hosts/h1/filesystem").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"name": ".bashrc", "path": "/home/bob/.bashrc", "type": "file"}]},
        )
    )
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    view = _last_update(client)
    assert view["callback_id"] == "omnigent_setup_select"
    # The workspace default is the host's home directory, not the bot's cwd.
    blocks = {b["block_id"]: b for b in view["blocks"] if "block_id" in b}
    assert blocks[WORKSPACE_BLOCK]["element"]["initial_value"] == "/home/bob"


@respx.mock
async def test_setup_shows_no_host_guidance_when_no_online_host(tmp_path: Path) -> None:
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/agents").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "ag_1", "name": "Helper"}]})
    )
    respx.get(_SERVER + "/v1/hosts").mock(
        return_value=httpx.Response(200, json={"hosts": [{"host_id": "h", "status": "offline"}]})
    )
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    # No online host → the guidance modal, not the agent/host select.
    view = _last_update(client)
    assert not any(b.get("block_id") == WORKSPACE_BLOCK for b in view["blocks"])
    body = view["blocks"][0]["text"]["text"]
    assert f"omni host --server {_SERVER}" in body
    assert "/omnigent" in body


@respx.mock
async def test_setup_shows_no_agents_guidance_when_server_has_no_agents(tmp_path: Path) -> None:
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/agents").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(_SERVER + "/v1/hosts").mock(
        return_value=httpx.Response(
            200, json={"hosts": [{"host_id": "h1", "name": "H", "status": "online"}]}
        )
    )
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    # No agents → a plain info screen, NOT the login-failure ("Login didn't
    # complete") wording that the errors branch would otherwise produce.
    view = _last_update(client)
    assert not any(b.get("block_id") == WORKSPACE_BLOCK for b in view["blocks"])
    body = view["blocks"][0]["text"]["text"]
    assert "no agents" in body.lower()
    assert "login" not in body.lower()
    assert _SERVER in body


@respx.mock
async def test_setup_shows_login_in_modal_and_advances_on_approval(tmp_path: Path) -> None:
    """Auth-enabled server: the modal shows the link, then advances on approval.

    No DM and no re-running /omnigent — login and config are one flow.
    """
    import asyncio

    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    # /v1/me → accounts mode, so login uses the device-grant flow.
    respx.get(_SERVER + "/v1/me").mock(
        return_value=httpx.Response(401, json={"login_url": "/login"})
    )
    # First /v1/agents (pre-login probe) 401s; after login it returns agents.
    agents_calls = {"n": 0}

    def _agents(request: httpx.Request) -> httpx.Response:
        agents_calls["n"] += 1
        if agents_calls["n"] == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"id": "ag_1", "name": "Helper"}]})

    respx.get(_SERVER + "/v1/agents").mock(side_effect=_agents)
    respx.get(_SERVER + "/v1/hosts").mock(
        return_value=httpx.Response(
            200, json={"hosts": [{"host_id": "h1", "name": "H", "status": "online"}]}
        )
    )
    respx.get(_SERVER + "/v1/hosts/h1/filesystem").mock(
        return_value=httpx.Response(
            200, json={"data": [{"name": ".x", "path": "/home/bob/.x", "type": "file"}]}
        )
    )
    authorize_route = respx.post(_SERVER + "/oauth/device/authorize").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "ABCD-2345",
                "verification_uri": _SERVER + "/oauth/device",
                "verification_uri_complete": (_SERVER + "/oauth/device?user_code=ABCD-2345"),
                "expires_in": 600,
                "interval": 0,
            },
        )
    )
    respx.post(_SERVER + "/oauth/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )
    )
    from cryptography.fernet import Fernet
    from omnigent_slack.auth_manager import AuthManager
    from omnigent_slack.tokens import EncryptedTokenStore

    token_store = EncryptedTokenStore(tmp_path / "tok.sqlite3", Fernet.generate_key().decode())
    await token_store.initialize()
    pool = OmnigentClientPool()
    auth = AuthManager(token_store)
    pool.set_auth_resolver(auth.resolve_auth)
    flow = _flow(await _store(tmp_path), pool, auth)
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")

        # The modal shows the login link in place (not a DM).
        waiting = client.updated_views[0]["view"]["blocks"][0]["text"]["text"]
        assert "ABCD-2345" in waiting
        assert client.posts == []  # no DM sent

        # client_id sent to the server is qualified by the workspace name
        # (from team.info → "Acme Corp").
        import json as _json

        authorize_body = _json.loads(authorize_route.calls.last.request.content)
        assert authorize_body["client_id"] == "Slack-Omnigent-Acme Corp"

        # The background poll approves and advances the SAME modal (views_update).
        for _ in range(50):
            if len(client.updated_views) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await pool.aclose_all()

    advanced = client.updated_views[-1]
    assert advanced["view_id"] == "V1"
    assert advanced["view"]["callback_id"] == "omnigent_setup_select"


@respx.mock
async def test_setup_auth_required_but_login_disabled(tmp_path: Path) -> None:
    """With no auth manager, an auth-enabled server shows a plain failure screen."""
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/agents").mock(return_value=httpx.Response(401))
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)  # no auth_manager
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    # Coherent failure screen in the modal — not a "check your DM" promise.
    body = _last_update(client)["blocks"][0]["text"]["text"]
    assert "isn't configured" in body
    assert client.posts == []


@respx.mock
async def test_setup_reports_device_grant_disabled(tmp_path: Path) -> None:
    """Accounts server with the device grant OFF (/oauth/* unmounted → 405):
    the modal must tell the user to contact the admin, not "try again shortly"."""
    from cryptography.fernet import Fernet
    from omnigent_slack.auth_manager import AuthManager
    from omnigent_slack.tokens import EncryptedTokenStore

    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    # /v1/me → accounts mode; the pre-login agents probe 401s so login starts.
    respx.get(_SERVER + "/v1/me").mock(
        return_value=httpx.Response(401, json={"login_url": "/login"})
    )
    respx.get(_SERVER + "/v1/agents").mock(return_value=httpx.Response(401))
    # Device grant disabled → authorize falls through to the SPA catch-all (405).
    respx.post(_SERVER + "/oauth/device/authorize").mock(return_value=httpx.Response(405))

    token_store = EncryptedTokenStore(tmp_path / "tok.sqlite3", Fernet.generate_key().decode())
    await token_store.initialize()
    pool = OmnigentClientPool()
    auth = AuthManager(token_store)
    pool.set_auth_resolver(auth.resolve_auth)
    flow = _flow(await _store(tmp_path), pool, auth)
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    body = _last_update(client)["blocks"][0]["text"]["text"].lower()
    assert "device authorization grant" in body
    assert "administrator" in body
    assert "try again shortly" not in body


@respx.mock
async def test_unknown_argument_opens_setup_modal(tmp_path: Path) -> None:
    """Any non-`logout` argument opens the setup modal (connecting screen)."""
    # The server is unreachable here; setup still opens the connecting modal
    # first, then updates it to a failure screen.
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(500))
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = FakeSetupClient()
    command = {
        "team_id": "T1",
        "user_id": "U1",
        "trigger_id": "trig-1",
        "text": "wat",
    }
    try:
        await flow._handle_config_command(FakeAck(), command, client)
    finally:
        await pool.aclose_all()

    assert len(client.opened_views) == 1
    assert client.opened_views[0]["view"]["callback_id"] == CALLBACK_SETUP_INFO


async def test_logout_revokes_all_and_clears_settings(tmp_path: Path) -> None:
    """`/omnigent logout` revokes every server token and clears saved data."""

    class FakeAuth:
        enabled = True

        def __init__(self) -> None:
            self.logged_out_all: list[tuple[str, str]] = []

        async def logout_all(self, team_id: str, user_id: str) -> int:
            self.logged_out_all.append((team_id, user_id))
            return 2

    store = await _store(tmp_path)
    # Seed config + an owned thread session so we can prove they're cleared.
    await store.upsert_user_config(
        "T1", "U1", UserConfig("ag_1", "Helper", "/home/bob", "h1", "H")
    )
    await store.upsert_session(ThreadKey("T1", "C1", "100.1"), "conv_1", "t", owner_user_id="U1")

    auth = FakeAuth()
    pool = OmnigentClientPool()
    flow = _flow(store, pool, auth)
    client = FakeSetupClient()
    command = {"team_id": "T1", "user_id": "U1", "text": "logout"}
    try:
        await flow._handle_config_command(FakeAck(), command, client)
    finally:
        await pool.aclose_all()

    assert auth.logged_out_all == [("T1", "U1")]
    assert await store.get_user_config("T1", "U1") is None
    assert await store.get_session(ThreadKey("T1", "C1", "100.1")) is None
    assert any("Logged out" in str(p.get("text", "")) for p in client.posts)


@respx.mock
async def test_setup_reports_unreachable(tmp_path: Path) -> None:
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(500))
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    body = _last_update(client)["blocks"][0]["text"]["text"]
    assert "reach" in body.lower()


async def test_select_submit_persists_config(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pool = OmnigentClientPool()
    flow = _flow(store, pool)
    ack = FakeAck()
    client = FakeSetupClient()

    view = {
        "state": {
            "values": {
                AGENT_BLOCK: {
                    "agent_select": {
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "Helper"},
                            "value": "ag_1",
                        }
                    }
                },
                HOST_BLOCK: {
                    "host_select": {
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "Host One"},
                            "value": "h1",
                        }
                    }
                },
                WORKSPACE_BLOCK: {"workspace_input": {"value": "/home/me/project"}},
            }
        },
    }
    body = {"team": {"id": "T1"}, "user": {"id": "U1"}}

    try:
        await flow._handle_select_submit(ack, body, view, client)
    finally:
        await pool.aclose_all()

    config = await store.get_user_config("T1", "U1")
    assert config is not None
    assert config.agent_id == "ag_1"
    assert config.workspace == "/home/me/project"
    assert config.host_id == "h1"
    assert config.host_name == "Host One"
    # Confirmation DM was posted.
    assert client.posts and "set up" in client.posts[0]["text"].lower()


async def test_select_submit_requires_a_host(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    pool = OmnigentClientPool()
    flow = _flow(store, pool)
    ack = FakeAck()
    client = FakeSetupClient()

    view = {
        "state": {
            "values": {
                AGENT_BLOCK: {
                    "agent_select": {
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "Helper"},
                            "value": "ag_1",
                        }
                    }
                },
                WORKSPACE_BLOCK: {"workspace_input": {"value": "/home/me/project"}},
            }
        },
    }
    body = {"team": {"id": "T1"}, "user": {"id": "U1"}}

    try:
        await flow._handle_select_submit(ack, body, view, client)
    finally:
        await pool.aclose_all()

    # No host selected → an inline error and nothing persisted.
    assert ack.calls[0]["response_action"] == "errors"
    assert HOST_BLOCK in ack.calls[0]["errors"]
    assert await store.get_user_config("T1", "U1") is None


def test_no_host_modal_shows_guidance() -> None:
    view = no_host_modal(_SERVER)
    assert view["callback_id"] == CALLBACK_SETUP_INFO
    body = view["blocks"][0]["text"]["text"]
    assert body == host_unavailable_text(_SERVER)
    assert f"omni host --server {_SERVER}" in body


def test_no_agents_modal_shows_guidance() -> None:
    view = no_agents_modal(_SERVER)
    assert view["callback_id"] == CALLBACK_SETUP_INFO
    body = view["blocks"][0]["text"]["text"]
    assert "no agents" in body.lower()
    assert _SERVER in body


def test_connecting_modal_is_info_only() -> None:
    view = connecting_modal()
    assert view["callback_id"] == CALLBACK_SETUP_INFO
    # No submit button — it's a progress screen driven by views_update.
    assert "submit" not in view


async def test_prompt_unconfigured_dms_and_pings_channel(tmp_path: Path) -> None:
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = FakeSetupClient()

    try:
        await flow.prompt_unconfigured(
            client, "U1", channel="C1", thread_ts="100.1", in_channel=True
        )
    finally:
        await pool.aclose_all()

    # A DM with the setup button and an ephemeral channel pointer.
    assert client.posts and client.posts[0]["channel"] == "D123"
    assert client.ephemeral and client.ephemeral[0]["channel"] == "C1"


async def test_prompt_unconfigured_handles_slack_response_object(tmp_path: Path) -> None:
    # The async web client returns a SlackResponse (not a dict); the DM channel
    # id must still be extracted so the setup button is actually delivered.
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = SlackResponseSetupClient()

    try:
        await flow.prompt_unconfigured(
            client, "U1", channel="C1", thread_ts=None, in_channel=False
        )
    finally:
        await pool.aclose_all()

    assert client.posts and client.posts[0]["channel"] == "D123"


async def test_config_command_opens_connecting_modal(tmp_path: Path) -> None:
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    ack = FakeAck()
    client = FakeSetupClient()

    try:
        await flow._handle_config_command(
            ack,
            {"trigger_id": "tid-1", "team_id": "T1", "user_id": "U1"},
            client,
        )
    finally:
        await pool.aclose_all()

    assert ack.calls == [{}]
    assert client.opened_views and client.opened_views[0]["trigger_id"] == "tid-1"
    assert client.opened_views[0]["view"]["callback_id"] == CALLBACK_SETUP_INFO
