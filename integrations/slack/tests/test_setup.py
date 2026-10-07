from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.setup import (
    ACTION_SETUP_START,
    AGENT_ACTION,
    AGENT_BLOCK,
    CALLBACK_SETUP_INFO,
    HOST_ACTION,
    HOST_BLOCK,
    MANAGED_HOST_VALUE,
    WORKSPACE_ACTION,
    WORKSPACE_BLOCK,
    SetupFlow,
    connecting_modal,
    host_unavailable_text,
    no_agents_modal,
    no_host_modal,
    select_modal,
    setup_failed_modal,
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

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "user": {"profile": {"email": "user@example.com"}}}


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


def _mock_info(*, managed: bool = False, provider: str | None = None) -> None:
    """Mock ``GET /v1/info``, the managed-sandbox capability probe ``validate`` reads.

    Every setup path that validates the server hits this, so each respx test that
    drives ``_begin_setup`` past ``/health`` declares it. Defaults to a server
    with no managed sandboxes — the pre-existing behavior.
    """
    respx.get(_SERVER + "/v1/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "managed_sandboxes_enabled": managed,
                "sandbox_provider": provider,
            },
        )
    )


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    return store


def _flow(
    store: SQLiteStore,
    pool: OmnigentClientPool,
    auth: Any = None,
    *,
    default_agent_id: str | None = None,
    default_host_type: str | None = None,
) -> SetupFlow:
    return SetupFlow(
        store=store,
        pool=pool,
        server_url=_SERVER,
        auth_manager=auth,
        default_agent_id=default_agent_id,
        default_host_type=default_host_type,
    )


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


def test_select_modal_offers_the_managed_sandbox_alongside_real_hosts() -> None:
    from omnigent_slack.omnigent import ValidatedServer

    view = select_modal(
        _SERVER,
        ValidatedServer(
            agents=[{"id": "ag_1", "name": "Helper"}],
            online_hosts=[{"host_id": "h1", "name": "Host One"}],
            managed_hosts=True,
            managed_host_provider="modal",
        ),
    )
    blocks = {b["block_id"]: b for b in view["blocks"] if "block_id" in b}
    host_opts = blocks[HOST_BLOCK]["element"]["options"]
    # The managed sandbox leads the menu, so a user with nothing online sees a
    # usable choice first; their own hosts follow.
    assert [o["value"] for o in host_opts] == [MANAGED_HOST_VALUE, "h1"]
    assert "modal" in host_opts[0]["text"]["text"]
    # The workspace field stops being required — a managed sandbox has no path.
    assert blocks[WORKSPACE_BLOCK]["optional"] is True


def test_select_modal_omits_managed_option_when_server_provisions_none() -> None:
    from omnigent_slack.omnigent import ValidatedServer

    view = select_modal(
        _SERVER,
        ValidatedServer(
            agents=[{"id": "ag_1", "name": "Helper"}],
            online_hosts=[{"host_id": "h1", "name": "Host One"}],
        ),
    )
    blocks = {b["block_id"]: b for b in view["blocks"] if "block_id" in b}
    assert [o["value"] for o in blocks[HOST_BLOCK]["element"]["options"]] == ["h1"]


def test_select_modal_leaves_workspace_blank_with_no_host_to_seed_it_from() -> None:
    # With only a managed sandbox on offer there is no host home to probe, and
    # the bot's own cwd names nothing the session can reach — so no default.
    from omnigent_slack.omnigent import ValidatedServer

    view = select_modal(
        _SERVER,
        ValidatedServer(agents=[{"id": "ag_1"}], online_hosts=[], managed_hosts=True),
    )
    blocks = {b["block_id"]: b for b in view["blocks"] if "block_id" in b}
    assert "initial_value" not in blocks[WORKSPACE_BLOCK]["element"]


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
    _mock_info()
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
    # No online host of the user's own AND no managed sandboxes on the server:
    # nothing can run a session, so the guidance screen is still the right end.
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/agents").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "ag_1", "name": "Helper"}]})
    )
    respx.get(_SERVER + "/v1/hosts").mock(
        return_value=httpx.Response(200, json={"hosts": [{"host_id": "h", "status": "offline"}]})
    )
    _mock_info(managed=False)
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
async def test_setup_offers_managed_sandbox_instead_of_dead_end(tmp_path: Path) -> None:
    # The reported failure: a user with no host of their own was hard-stopped and
    # told to run `omni host` on their own machine. With managed sandboxes on the
    # server, setup must reach the picker instead, offering the sandbox.
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/agents").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "ag_1", "name": "Helper"}]})
    )
    respx.get(_SERVER + "/v1/hosts").mock(
        return_value=httpx.Response(200, json={"hosts": [{"host_id": "h", "status": "offline"}]})
    )
    _mock_info(managed=True, provider="modal")
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    view = _last_update(client)
    assert view["callback_id"] == "omnigent_setup_select"
    blocks = {b["block_id"]: b for b in view["blocks"] if "block_id" in b}
    # The sandbox is the only host on offer, and no `omni host` guidance is shown.
    host_opts = blocks[HOST_BLOCK]["element"]["options"]
    assert [o["value"] for o in host_opts] == [MANAGED_HOST_VALUE]
    assert "omni host --server" not in str(view)


@respx.mock
async def test_setup_shows_no_agents_guidance_when_server_has_no_agents(tmp_path: Path) -> None:
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/agents").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(_SERVER + "/v1/hosts").mock(
        return_value=httpx.Response(
            200, json={"hosts": [{"host_id": "h1", "name": "H", "status": "online"}]}
        )
    )
    _mock_info()
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
    _mock_info()
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


class _EnrollAuth:
    """Minimal auth manager stub for the Databricks enrollment path."""

    enabled = True

    def __init__(self) -> None:
        self.awaited: list[dict[str, Any]] = []

    def await_enrollment_in_background(self, **kwargs: Any) -> None:
        self.awaited.append(kwargs)


def _enroll_flow(
    store: SQLiteStore, pool: OmnigentClientPool, auth: Any, enrollment_url: Any
) -> SetupFlow:
    return SetupFlow(
        store=store,
        pool=pool,
        server_url=_SERVER,
        auth_manager=auth,
        enrollment_url=enrollment_url,
    )


async def test_databricks_enrollment_shows_link_bound_to_slack_email(tmp_path: Path) -> None:
    # The enrollment link must be built from the user's Slack email (looked up
    # via users.info), so the callback can bind it to X-Forwarded-Email.
    seen: list[tuple[str, str, str, str]] = []

    def _url(team_id: str, user_id: str, email: str, team_name: str = "") -> str:
        seen.append((team_id, user_id, email, team_name))
        return f"https://bot.example.com/auth/callback?state=signed-{email}"

    auth = _EnrollAuth()
    pool = OmnigentClientPool()
    flow = _enroll_flow(await _store(tmp_path), pool, auth, _url)
    client = FakeSetupClient()
    try:
        await flow._begin_databricks_enrollment(
            client, team_id="T1", user_id="U1", server_url=_SERVER, view_id="V1"
        )
    finally:
        await pool.aclose_all()

    # Email + workspace name were resolved from Slack and passed to the minter.
    assert seen == [("T1", "U1", "user@example.com", "Acme Corp")]
    # The waiting modal shows the signed link and the poll was started.
    body = _last_update(client)["blocks"][0]["text"]["text"]
    assert "auth/callback?state=signed-user@example.com" in body
    assert len(auth.awaited) == 1


@respx.mock
async def test_post_enrollment_advance_uses_freshly_stored_token(tmp_path: Path) -> None:
    # Regression: setup pools a TOKENLESS client during the pre-login probe. The
    # pool resolves auth only at client creation, so after enrollment stores a
    # token the cached client still has none — its re-validate re-hits the auth
    # wall and the modal stalls on "requires authentication". _on_success must
    # invalidate that cached client so the re-fetch picks up the new token.
    from omnigent_slack.omnigent import ClientAuth

    # A mutable token that only appears after "enrollment".
    token_box: dict[str, str | None] = {"token": None}

    async def _resolver(server_url: str, user_id: str) -> ClientAuth | None:
        tok = token_box["token"]
        return ClientAuth(tok, lambda: _noop()) if tok else None

    async def _noop() -> str | None:
        return None

    # /health and the listing endpoints require the bearer: 401 without it, 200
    # with it — mirroring an auth-gated server.
    def _needs_auth(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == "Bearer real-token":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(401)

    respx.get(_SERVER + "/health").mock(side_effect=_needs_auth)
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
    _mock_info()

    def _url(team_id: str, user_id: str, email: str, team_name: str = "") -> str:
        return "https://bot/callback"

    auth = _EnrollAuth()
    pool = OmnigentClientPool()
    pool.set_auth_resolver(_resolver)
    flow = _enroll_flow(await _store(tmp_path), pool, auth, _url)
    client = FakeSetupClient()

    try:
        # Full flow: _begin_setup probes the server (pooling a TOKENLESS client
        # when validate 401s), which routes into the Databricks enrollment path.
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
        # Enrollment stores the token, then fires the success hook.
        token_box["token"] = "real-token"
        on_success = auth.awaited[-1]["on_success"]
        await on_success()
    finally:
        await pool.aclose_all()

    # The modal advanced to the agent/host select rather than stalling.
    view = _last_update(client)
    assert view["callback_id"] == "omnigent_setup_select"


async def test_databricks_enrollment_fails_closed_without_email(tmp_path: Path) -> None:
    # If Slack won't give us the email (missing users:read.email scope), we must
    # NOT issue an unverifiable link — show an error and don't start the poll.
    class NoEmailClient(FakeSetupClient):
        async def users_info(self, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "user": {"profile": {}}}

    def _url(*args: Any, **kwargs: Any) -> str:  # pragma: no cover - must not be called
        raise AssertionError("enrollment_url must not be called without an email")

    auth = _EnrollAuth()
    pool = OmnigentClientPool()
    flow = _enroll_flow(await _store(tmp_path), pool, auth, _url)
    client = NoEmailClient()
    try:
        await flow._begin_databricks_enrollment(
            client, team_id="T1", user_id="U1", server_url=_SERVER, view_id="V1"
        )
    finally:
        await pool.aclose_all()

    body = _last_update(client)["blocks"][0]["text"]["text"]
    assert "email" in body.lower()
    assert auth.awaited == []


@respx.mock
async def test_post_enrollment_validate_failure_shows_error_not_hang(tmp_path: Path) -> None:
    # Regression: the callback stored a token (browser shows "You're connected"),
    # but validating it against the server fails — e.g. the granted scope isn't
    # accepted. The modal must show a failure screen, not hang on "waiting".
    from omnigent_slack.omnigent import ClientAuth

    async def _resolver(server_url: str, user_id: str) -> ClientAuth | None:
        return ClientAuth("stored-but-rejected", lambda: _noop())

    async def _noop() -> str | None:
        return None

    # The server keeps rejecting the token even after it's stored.
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(401))

    def _url(team_id: str, user_id: str, email: str, team_name: str = "") -> str:
        return "https://bot/callback"

    auth = _EnrollAuth()
    pool = OmnigentClientPool()
    pool.set_auth_resolver(_resolver)
    flow = _enroll_flow(await _store(tmp_path), pool, auth, _url)
    client = FakeSetupClient()

    try:
        await flow._begin_databricks_enrollment(
            client, team_id="T1", user_id="U1", server_url=_SERVER, view_id="V1"
        )
        # Fire the success hook as the poll would once the token lands.
        on_success = auth.awaited[-1]["on_success"]
        await on_success()
    finally:
        await pool.aclose_all()

    view = _last_update(client)
    # Not stuck on the waiting screen, and not advanced to select — a clear error.
    assert view["callback_id"] != "omnigent_setup_select"
    body = view["blocks"][0]["text"]["text"]
    assert "didn't complete" in body.lower() or "rejected" in body.lower()


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


async def test_select_submit_persists_managed_choice_without_a_workspace(tmp_path: Path) -> None:
    # Picking the managed sandbox stores host_type="managed" with no host id and
    # no workspace — the server chooses both. The stale path left in the (now
    # optional) workspace box must not be persisted or block the submit.
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
                            "text": {"type": "plain_text", "text": "Managed sandbox (modal)"},
                            "value": MANAGED_HOST_VALUE,
                        }
                    }
                },
                WORKSPACE_BLOCK: {"workspace_input": {"value": "/left/over/path"}},
            }
        },
    }
    body = {"team": {"id": "T1"}, "user": {"id": "U1"}}

    try:
        await flow._handle_select_submit(ack, body, view, client)
    finally:
        await pool.aclose_all()

    assert ack.calls == [{}]  # saved, not an inline error
    config = await store.get_user_config("T1", "U1")
    assert config is not None
    assert config.host_type == "managed"
    assert config.host_id is None
    assert config.workspace == ""
    assert client.posts and "sandbox" in client.posts[0]["text"].lower()


async def test_select_submit_still_requires_a_path_for_a_real_host(tmp_path: Path) -> None:
    # Making the workspace input optional is for the managed sandbox only — an
    # external host still can't start a runner without an absolute path.
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
                WORKSPACE_BLOCK: {"workspace_input": {"value": ""}},
            }
        },
    }
    body = {"team": {"id": "T1"}, "user": {"id": "U1"}}

    try:
        await flow._handle_select_submit(ack, body, view, client)
    finally:
        await pool.aclose_all()

    assert ack.calls[0]["response_action"] == "errors"
    assert WORKSPACE_BLOCK in ack.calls[0]["errors"]
    assert await store.get_user_config("T1", "U1") is None


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


@pytest.mark.parametrize(
    "body",
    [
        {"user": {"id": "U1"}},
        {"team": {"id": "T1"}, "user": {}},
    ],
    ids=["missing-team", "missing-user"],
)
async def test_select_submit_fails_closed_on_missing_team_or_user(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    # A submission Slack can't attribute to a (team, user) can't be stored, so
    # the modal must say so instead of closing like a save. Every field below
    # is valid: the identity, not the form, is what fails.
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

    try:
        await flow._handle_select_submit(ack, body, view, client)
    finally:
        await pool.aclose_all()

    # The modal names the failure instead of closing like a save.
    assert len(ack.calls) == 1
    assert ack.calls[0]["response_action"] == "update"
    failed = ack.calls[0]["view"]["blocks"][0]["text"]["text"]
    assert "wasn't saved" in failed
    assert "/omnigent" in failed
    # Nothing stored under either key a blank half would collapse to.
    assert await store.get_user_config("", "U1") is None
    assert await store.get_user_config("T1", "") is None
    # And no "You're set up!" DM claiming otherwise.
    assert client.posts == []


def test_setup_failed_modal_shows_guidance() -> None:
    view = setup_failed_modal("Slack didn't say who you are.")
    assert view["callback_id"] == CALLBACK_SETUP_INFO
    # No submit — the picker is gone, so there is nothing left to resubmit.
    assert "submit" not in view
    body = view["blocks"][0]["text"]["text"]
    assert "Slack didn't say who you are." in body
    assert "/omnigent" in body


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


@respx.mock
async def test_setup_settles_modal_before_first_update(tmp_path: Path, monkeypatch: Any) -> None:
    # The just-opened modal must settle on the client before the first
    # views_update, or Slack accepts the update (ok:true) while the not-yet-
    # rendered client drops it and the modal hangs on "Connecting…". Assert the
    # settle sleep runs, and runs BEFORE any views_update fires.
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
        return_value=httpx.Response(200, json={"data": []})
    )
    _mock_info()

    events: list[str] = []

    import omnigent_slack.setup as setup_mod

    real_sleep = setup_mod.asyncio.sleep

    async def _tracking_sleep(delay: float) -> None:
        # Only the settle sleep (>0) is interesting; don't record 0-delay yields.
        if delay > 0:
            events.append(f"sleep:{delay}")
        await real_sleep(0)

    monkeypatch.setattr(setup_mod.asyncio, "sleep", _tracking_sleep)

    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)

    class _RecordingClient(FakeSetupClient):
        async def views_update(self, **kwargs: Any) -> dict[str, Any]:
            events.append("views_update")
            return await super().views_update(**kwargs)

    client = _RecordingClient()

    try:
        await flow._handle_config_command(
            FakeAck(),
            {"trigger_id": "tid-1", "team_id": "T1", "user_id": "U1"},
            client,
        )
    finally:
        await pool.aclose_all()

    assert f"sleep:{setup_mod._MODAL_SETTLE_SECONDS}" in events
    # The settle sleep precedes the first views_update.
    assert events.index(f"sleep:{setup_mod._MODAL_SETTLE_SECONDS}") < events.index("views_update")


async def test_config_command_fails_closed_on_missing_team(tmp_path: Path) -> None:
    # An empty team_id would collapse token/config keys across workspaces, so the
    # slash-command path must fail closed (ack, then nothing) rather than proceed.
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    ack = FakeAck()
    client = FakeSetupClient()

    try:
        await flow._handle_config_command(
            ack,
            {"trigger_id": "tid-1", "team_id": "", "user_id": "U1"},
            client,
        )
    finally:
        await pool.aclose_all()

    assert ack.calls == [{}]
    assert client.opened_views == []


async def test_setup_start_button_fails_closed_on_missing_team(tmp_path: Path) -> None:
    # The button-driven setup path must apply the same empty-team/user fail-closed
    # guard as the slash-command path (keys collapse across workspaces otherwise).
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    ack = FakeAck()
    client = FakeSetupClient()

    try:
        await flow._handle_setup_start(
            ack,
            {"trigger_id": "tid-1", "team": {"id": ""}, "user": {"id": "U1"}},
            client,
        )
    finally:
        await pool.aclose_all()

    assert ack.calls == [{}]
    assert client.opened_views == []


async def test_prompt_relogin_dms_setup_button(tmp_path: Path) -> None:
    # An expired-token user gets a DM carrying the re-login setup button — reliably
    # delivered and actionable — plus an in-channel ephemeral pointer to the DM.
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = FakeSetupClient()

    try:
        delivered = await flow.prompt_relogin(
            client, "U1", channel="C1", thread_ts="100.1", in_channel=True
        )
    finally:
        await pool.aclose_all()

    assert delivered is True
    # The DM landed on the opened DM channel and carries the setup-start button.
    assert client.posts and client.posts[-1]["channel"] == "D123"
    dm = client.posts[-1]
    assert "expired" in dm["text"].lower()
    action_ids = [
        el.get("action_id") for block in dm.get("blocks", []) for el in block.get("elements", [])
    ]
    assert ACTION_SETUP_START in action_ids
    # The channel trigger also nudges the user to their DM.
    assert client.ephemeral and client.ephemeral[-1]["user"] == "U1"


async def test_prompt_relogin_reports_when_dm_cannot_open(tmp_path: Path) -> None:
    # If the DM channel can't be opened, prompt_relogin reports failure (no post)
    # so the caller can log it rather than assume delivery.
    class NoDmClient(FakeSetupClient):
        async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
            return {"channel": {}}  # no id

    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool)
    client = NoDmClient()

    try:
        delivered = await flow.prompt_relogin(
            client, "U1", channel="C1", thread_ts="100.1", in_channel=False
        )
    finally:
        await pool.aclose_all()

    assert delivered is False
    assert client.posts == []


# ── Operator-set setup defaults ──────────────────────────────────────


def _validated(
    *,
    agents: list[dict[str, Any]] | None = None,
    managed: bool = False,
    managed_support_known: bool = True,
) -> Any:
    from omnigent_slack.omnigent import ValidatedServer

    return ValidatedServer(
        agents=agents if agents is not None else [{"id": "ag_1", "name": "Helper"}],
        online_hosts=[{"host_id": "h1", "name": "Host One"}],
        managed_hosts=managed,
        managed_host_provider="modal" if managed else None,
        managed_support_known=managed_support_known,
    )


def _element(view: dict[str, Any], block_id: str) -> dict[str, Any]:
    blocks = {b["block_id"]: b for b in view["blocks"] if "block_id" in b}
    element: dict[str, Any] = blocks[block_id]["element"]
    return element


def _notes(view: dict[str, Any]) -> list[str]:
    """Text of every context note the modal carries (the unavailable-default ones)."""
    return [
        str(element.get("text", ""))
        for block in view["blocks"]
        if block.get("type") == "context"
        for element in block.get("elements", [])
    ]


# The token the setup tests authenticate with. Against a fake that ignores
# credentials an unauthenticated client (the pool's ``auth=None`` path) is
# indistinguishable from a real one, so auth-gated listings are gated here too.
_BEARER = "at"


def _bearer_gated(payload: dict[str, Any]) -> Any:
    """respx side_effect serving ``payload`` only to a request carrying ``_BEARER``."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {_BEARER}":
            return httpx.Response(401)
        return httpx.Response(200, json=payload)

    return _handler


async def _authenticated_pool(tmp_path: Path, *, token: str | None = _BEARER) -> Any:
    """A pool whose resolver hands out ``token`` for (T1, U1) on ``_SERVER``."""
    from cryptography.fernet import Fernet
    from omnigent_slack.auth_manager import AuthManager
    from omnigent_slack.tokens import EncryptedTokenStore

    token_store = EncryptedTokenStore(tmp_path / "tok.sqlite3", Fernet.generate_key().decode())
    await token_store.initialize()
    if token is not None:
        await token_store.put("T1", "U1", _SERVER, access_token=token, refresh_token="rt")
    pool = OmnigentClientPool()
    auth = AuthManager(token_store)
    pool.set_auth_resolver(auth.resolve_auth)
    return pool, auth


def _state_as_submitted(
    view: dict[str, Any],
    *,
    agent: dict[str, Any] | None = None,
    host: dict[str, Any] | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """The ``view.state`` Slack posts back for ``view``, honoring its pre-selections.

    An untouched ``static_select`` submits its ``initial_option``; ``agent`` /
    ``host`` stand in for the user picking something else instead. Derived from
    the rendered view rather than hand-written, so a lost pre-selection shows up
    here as an empty submission instead of passing anyway.
    """
    values: dict[str, Any] = {}
    for block_id, action_id, override in (
        (AGENT_BLOCK, AGENT_ACTION, agent),
        (HOST_BLOCK, HOST_ACTION, host),
    ):
        values[block_id] = {
            action_id: {
                "selected_option": override or _element(view, block_id).get("initial_option")
            }
        }
    typed = (
        workspace
        if workspace is not None
        else _element(view, WORKSPACE_BLOCK).get("initial_value", "")
    )
    values[WORKSPACE_BLOCK] = {WORKSPACE_ACTION: {"value": typed}}
    return {"state": {"values": values}}


def test_select_modal_renders_the_unchanged_payload_when_no_defaults_are_set() -> None:
    """The guard that matters most: configuring nothing changes nothing.

    Checked as payload equivalence, not just "no initial_option" — the block
    sequence, the modal's own keys, and the absence of the string anywhere in
    the serialized view, so any block this feature might add shows up here.
    """
    import json

    view = select_modal(_SERVER, _validated(managed=True))
    assert [b["type"] for b in view["blocks"]] == ["section", "input", "input", "input"]
    assert [b.get("block_id") for b in view["blocks"]] == [
        None,
        AGENT_BLOCK,
        HOST_BLOCK,
        WORKSPACE_BLOCK,
    ]
    assert set(view) == {"type", "callback_id", "title", "submit", "close", "blocks"}
    assert "initial_option" not in json.dumps(view)
    assert _notes(view) == []


def test_select_modal_preselects_the_configured_default_agent() -> None:
    view = select_modal(
        _SERVER,
        _validated(agents=[{"id": "ag_1", "name": "Helper"}, {"id": "ag_2", "name": "Other"}]),
        default_agent_id="ag_2",
    )
    element = _element(view, AGENT_BLOCK)
    assert element["initial_option"]["value"] == "ag_2"
    # Slack only accepts an initial_option that is one of the menu's options.
    assert element["initial_option"] in element["options"]
    # Nothing is skipped or removed — the user can still pick the other agent.
    assert [o["value"] for o in element["options"]] == ["ag_1", "ag_2"]
    assert view["submit"]["text"] == "Save"
    assert _notes(view) == []


def test_select_modal_preselects_the_managed_sandbox_when_configured() -> None:
    view = select_modal(_SERVER, _validated(managed=True), default_host_type="managed")
    element = _element(view, HOST_BLOCK)
    assert element["initial_option"]["value"] == MANAGED_HOST_VALUE
    assert element["initial_option"] in element["options"]
    # The user's own host is still on offer.
    assert [o["value"] for o in element["options"]] == [MANAGED_HOST_VALUE, "h1"]
    assert _notes(view) == []


def test_select_modal_leaves_agent_blank_when_the_default_is_not_offered() -> None:
    # The configured agent isn't among the ones this user is offered: say so and
    # leave the menu blank. Substituting a different agent would silently put
    # someone on the wrong one.
    view = select_modal(_SERVER, _validated(), default_agent_id="ag_missing")
    element = _element(view, AGENT_BLOCK)
    assert "initial_option" not in element
    assert [o["value"] for o in element["options"]] == ["ag_1"]
    assert any("ag_missing" in note for note in _notes(view))


def test_select_modal_leaves_host_blank_when_the_server_provisions_no_sandbox() -> None:
    # Managed capability gates the pre-selection: a server with no managed
    # sandboxes offers no such option, so there is nothing to pre-select.
    view = select_modal(_SERVER, _validated(managed=False), default_host_type="managed")
    element = _element(view, HOST_BLOCK)
    assert "initial_option" not in element
    assert MANAGED_HOST_VALUE not in [o["value"] for o in element["options"]]
    # The server answered "no", so saying so is a fact we established.
    assert any("doesn't provision one" in note for note in _notes(view))


def test_select_modal_keeps_the_usable_default_when_the_other_is_unavailable() -> None:
    # A partly-honorable config still honors the part it can.
    view = select_modal(
        _SERVER,
        _validated(managed=False),
        default_agent_id="ag_1",
        default_host_type="managed",
    )
    assert _element(view, AGENT_BLOCK)["initial_option"]["value"] == "ag_1"
    assert "initial_option" not in _element(view, HOST_BLOCK)
    assert len(_notes(view)) == 1


def test_select_modal_does_not_preselect_an_agent_beyond_the_option_cap() -> None:
    # Slack caps a static_select at 100 options, so agent 101 is not in the menu
    # and cannot be pre-selected. That is the unavailable case (blank + a note),
    # not a silent no-op the operator has no way to notice.
    agents = [{"id": f"ag_{n}", "name": f"Agent {n}"} for n in range(101)]
    view = select_modal(_SERVER, _validated(agents=agents), default_agent_id="ag_100")
    element = _element(view, AGENT_BLOCK)
    assert len(element["options"]) == 100
    assert "initial_option" not in element
    assert any("ag_100" in note for note in _notes(view))


@respx.mock
async def test_setup_preselects_defaults_resolved_as_the_authenticated_user(
    tmp_path: Path,
) -> None:
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    # Auth-gated exactly as the real listing endpoints are: an unauthenticated
    # client sees a 401, so reaching the picker at all proves the user's own
    # token resolved before the defaults were matched against anything.
    respx.get(_SERVER + "/v1/agents").mock(
        side_effect=_bearer_gated(
            {"data": [{"id": "ag_1", "name": "Helper"}, {"id": "ag_2", "name": "Other"}]}
        )
    )
    respx.get(_SERVER + "/v1/hosts").mock(
        side_effect=_bearer_gated({"hosts": [{"host_id": "h1", "name": "H", "status": "online"}]})
    )
    respx.get(_SERVER + "/v1/hosts/h1/filesystem").mock(
        side_effect=_bearer_gated(
            {"data": [{"name": ".x", "path": "/home/bob/.x", "type": "file"}]}
        )
    )
    _mock_info(managed=True, provider="modal")
    pool, auth = await _authenticated_pool(tmp_path)
    flow = _flow(
        await _store(tmp_path), pool, auth, default_agent_id="ag_2", default_host_type="managed"
    )
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    view = _last_update(client)
    assert view["callback_id"] == "omnigent_setup_select"
    assert _element(view, AGENT_BLOCK)["initial_option"]["value"] == "ag_2"
    assert _element(view, HOST_BLOCK)["initial_option"]["value"] == MANAGED_HOST_VALUE


@respx.mock
async def test_setup_defaults_are_resolved_after_the_user_logs_in(tmp_path: Path) -> None:
    """The default is matched against the listing fetched as the logged-in user.

    The pre-login probe 401s and lists nothing, so a default resolved before
    auth could only ever come back "unavailable". Pre-selection appearing after
    approval is what proves it is resolved on the post-login listing.
    """
    import asyncio

    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/me").mock(
        return_value=httpx.Response(401, json={"login_url": "/login"})
    )
    # Gated on the bearer rather than on call ordering: the pre-login probe 401s
    # because it carries no token, and the post-login listing succeeds because
    # the device grant stored one. A fake that ignored credentials would pass
    # even if the pooled client were never given the user's auth at all.
    respx.get(_SERVER + "/v1/agents").mock(
        side_effect=_bearer_gated({"data": [{"id": "ag_1", "name": "Helper"}]})
    )
    respx.get(_SERVER + "/v1/hosts").mock(
        side_effect=_bearer_gated({"hosts": [{"host_id": "h1", "name": "H", "status": "online"}]})
    )
    respx.get(_SERVER + "/v1/hosts/h1/filesystem").mock(
        side_effect=_bearer_gated(
            {"data": [{"name": ".x", "path": "/home/bob/.x", "type": "file"}]}
        )
    )
    _mock_info()
    respx.post(_SERVER + "/oauth/device/authorize").mock(
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
            200, json={"access_token": _BEARER, "refresh_token": "rt", "expires_in": 3600}
        )
    )
    # No token stored up front — the device grant above is what puts one there.
    pool, auth = await _authenticated_pool(tmp_path, token=None)
    flow = _flow(await _store(tmp_path), pool, auth, default_agent_id="ag_1")
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
        for _ in range(50):
            if len(client.updated_views) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await pool.aclose_all()

    view = _last_update(client)
    assert view["callback_id"] == "omnigent_setup_select"
    assert _element(view, AGENT_BLOCK)["initial_option"]["value"] == "ag_1"
    assert _notes(view) == []


@respx.mock
async def test_unreachable_server_is_not_reported_as_an_unavailable_default(
    tmp_path: Path,
) -> None:
    # A network failure says nothing about whether the configured agent exists,
    # so it must still be the try-again screen — never "your default is gone".
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(500))
    pool = OmnigentClientPool()
    flow = _flow(
        await _store(tmp_path), pool, default_agent_id="ag_1", default_host_type="managed"
    )
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    view = _last_update(client)
    assert view["callback_id"] == CALLBACK_SETUP_INFO
    body = view["blocks"][0]["text"]["text"]
    assert "reach" in body.lower()
    assert "ag_1" not in str(view)


@respx.mock
async def test_a_server_with_no_agents_still_shows_the_no_agents_screen(tmp_path: Path) -> None:
    # Nothing to pick means nothing to default to: keep the existing guidance
    # screen rather than a picker whose only content is a warning.
    respx.get(_SERVER + "/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(_SERVER + "/v1/agents").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(_SERVER + "/v1/hosts").mock(
        return_value=httpx.Response(
            200, json={"hosts": [{"host_id": "h1", "name": "H", "status": "online"}]}
        )
    )
    _mock_info()
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool, default_agent_id="ag_1")
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    view = _last_update(client)
    assert view["callback_id"] == CALLBACK_SETUP_INFO
    assert "no agents available" in view["blocks"][0]["text"]["text"]


async def test_submitting_the_preselected_managed_default_stores_no_host_or_workspace(
    tmp_path: Path,
) -> None:
    # Accepting both defaults untouched must persist exactly what a hand-picked
    # managed sandbox does: no host id, no workspace (the server owns both).
    store = await _store(tmp_path)
    pool = OmnigentClientPool()
    flow = _flow(store, pool, default_agent_id="ag_1", default_host_type="managed")
    view = select_modal(
        _SERVER,
        _validated(managed=True),
        workspace_default="/home/bob",
        default_agent_id=flow._default_agent_id,
        default_host_type=flow._default_host_type,
    )
    ack = FakeAck()
    client = FakeSetupClient()

    try:
        await flow._handle_select_submit(
            ack, {"team": {"id": "T1"}, "user": {"id": "U1"}}, _state_as_submitted(view), client
        )
    finally:
        await pool.aclose_all()

    config = await store.get_user_config("T1", "U1")
    assert config is not None
    assert config.agent_id == "ag_1"
    assert config.host_type == "managed"
    assert config.host_id is None
    assert config.workspace == ""


async def test_an_explicit_user_choice_wins_over_the_preselected_defaults(
    tmp_path: Path,
) -> None:
    """A submitted choice beats the operator's default — on a flow that HAS one.

    The flow itself is configured with both defaults, so a submit handler that
    preferred ``self._default_agent_id`` over the submitted option would have
    something to prefer, and this test would catch it. Built without them, it
    could not.
    """
    store = await _store(tmp_path)
    pool = OmnigentClientPool()
    flow = _flow(store, pool, default_agent_id="ag_1", default_host_type="managed")
    view = select_modal(
        _SERVER,
        _validated(
            agents=[{"id": "ag_1", "name": "Helper"}, {"id": "ag_2", "name": "Other"}],
            managed=True,
        ),
        default_agent_id=flow._default_agent_id,
        default_host_type=flow._default_host_type,
    )
    # The defaults really are pre-selected — otherwise "the user overrode them"
    # would be a claim about a modal that never offered them.
    assert _element(view, AGENT_BLOCK)["initial_option"]["value"] == "ag_1"
    assert _element(view, HOST_BLOCK)["initial_option"]["value"] == MANAGED_HOST_VALUE

    agent_options = _element(view, AGENT_BLOCK)["options"]
    host_options = _element(view, HOST_BLOCK)["options"]
    state = _state_as_submitted(
        view,
        agent=next(o for o in agent_options if o["value"] == "ag_2"),
        host=next(o for o in host_options if o["value"] == "h1"),
        workspace="/home/me/project",
    )
    ack = FakeAck()
    client = FakeSetupClient()

    try:
        await flow._handle_select_submit(
            ack, {"team": {"id": "T1"}, "user": {"id": "U1"}}, state, client
        )
    finally:
        await pool.aclose_all()

    config = await store.get_user_config("T1", "U1")
    assert config is not None
    assert config.agent_id == "ag_2"
    assert config.host_id == "h1"
    assert config.host_type == "external"
    assert config.workspace == "/home/me/project"


async def test_switching_off_the_managed_default_still_requires_a_workspace_path(
    tmp_path: Path,
) -> None:
    # Moving off the pre-selected sandbox to a real host re-imposes the absolute
    # path requirement; the defaults path must not loosen it.
    store = await _store(tmp_path)
    pool = OmnigentClientPool()
    flow = _flow(store, pool, default_agent_id="ag_1", default_host_type="managed")
    view = select_modal(
        _SERVER,
        _validated(managed=True),
        default_agent_id=flow._default_agent_id,
        default_host_type=flow._default_host_type,
    )
    host_options = _element(view, HOST_BLOCK)["options"]
    state = _state_as_submitted(
        view, host=next(o for o in host_options if o["value"] == "h1"), workspace="relative/path"
    )
    ack = FakeAck()
    client = FakeSetupClient()

    try:
        await flow._handle_select_submit(
            ack, {"team": {"id": "T1"}, "user": {"id": "U1"}}, state, client
        )
    finally:
        await pool.aclose_all()

    assert ack.calls[-1]["response_action"] == "errors"
    assert WORKSPACE_BLOCK in ack.calls[-1]["errors"]
    assert await store.get_user_config("T1", "U1") is None


def test_an_unreadable_capability_probe_is_not_reported_as_non_support() -> None:
    # An unreadable `/v1/info` is not the server answering "no". The option is
    # still withheld (offering one the server would 422 is worse), but the modal
    # must not report the failure to ask as a finding about the server.
    view = select_modal(
        _SERVER,
        _validated(managed=False, managed_support_known=False),
        default_host_type="managed",
    )
    element = _element(view, HOST_BLOCK)
    assert "initial_option" not in element
    assert MANAGED_HOST_VALUE not in [o["value"] for o in element["options"]]
    notes = _notes(view)
    assert any("couldn't be checked" in note for note in notes)
    assert not any("doesn't provision one" in note for note in notes)


def test_an_agent_past_the_option_cap_is_named_as_a_menu_limit() -> None:
    # Agent 101 exists on the server and is perfectly usable — it just didn't fit
    # the menu. Saying "this server doesn't offer it" would be a false diagnosis.
    agents = [{"id": f"ag_{n}", "name": f"Agent {n}"} for n in range(101)]
    view = select_modal(_SERVER, _validated(agents=agents), default_agent_id="ag_100")
    note = next(n for n in _notes(view) if "ag_100" in n)
    assert "isn't available in this menu" in note
    assert "first 100 of 101 agents" in note


def test_each_unavailable_default_note_follows_its_own_picker() -> None:
    # Each note says "above", so each must sit AFTER the picker it refers to and
    # before the next one — otherwise the wording sends the reader the wrong way.
    view = select_modal(
        _SERVER,
        _validated(managed=False),
        default_agent_id="ag_missing",
        default_host_type="managed",
    )
    assert len(_notes(view)) == 2
    assert all("above" in note and "below" not in note for note in _notes(view))

    def _at(predicate: Any) -> int:
        return next(i for i, block in enumerate(view["blocks"]) if predicate(block))

    def _note_saying(text: str) -> Any:
        return lambda b: b["type"] == "context" and text in str(b)

    agent_picker = _at(lambda b: b.get("block_id") == AGENT_BLOCK)
    host_picker = _at(lambda b: b.get("block_id") == HOST_BLOCK)
    agent_note = _at(_note_saying("ag_missing"))
    host_note = _at(_note_saying("managed sandbox"))
    # Each note is below its own picker…
    assert agent_picker < agent_note
    assert host_picker < host_note
    # …and the agent's note stays above the host picker, so it can't be read as
    # belonging to the menu underneath it.
    assert agent_note < host_picker


@respx.mock
async def test_an_unreachable_capability_probe_does_not_claim_non_support(
    tmp_path: Path,
) -> None:
    """The reviewer's reproduction: everything works except `/v1/info`.

    Health, agents and an online external host all succeed, so setup reaches the
    picker — but the managed capability was never established. The modal must
    not diagnose the operator's server from a probe we could not read.
    """
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
            200, json={"data": [{"name": ".x", "path": "/home/bob/.x", "type": "file"}]}
        )
    )
    respx.get(_SERVER + "/v1/info").mock(return_value=httpx.Response(500))
    pool = OmnigentClientPool()
    flow = _flow(await _store(tmp_path), pool, default_host_type="managed")
    client = FakeSetupClient()

    try:
        await flow._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await pool.aclose_all()

    view = _last_update(client)
    assert view["callback_id"] == "omnigent_setup_select"
    notes = _notes(view)
    assert any("couldn't be checked" in note for note in notes)
    assert not any("doesn't provision one" in note for note in notes)
    # The option is still withheld — a create against it would 422.
    assert MANAGED_HOST_VALUE not in [o["value"] for o in _element(view, HOST_BLOCK)["options"]]
