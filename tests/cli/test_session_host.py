"""Session host affinity and its integration with CLI authentication."""

from __future__ import annotations

import uuid

import httpx
import pytest

from omnigent.chat import _DatabricksTokenAuth
from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_sdk_client
from omnigent.session_host import get_session_host, set_session_host

OLD_SESSION = "11111111111111111111111111111111"
NEW_SESSION = "22222222222222222222222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_native_clients_keep_attachment_owner_after_prepare(
    monkeypatch: pytest.MonkeyPatch,
    scheme: str,
) -> None:
    """New HTTP clients and WS reconnects preserve the discovered owner."""
    from omnigent.cli_auth import open_server_client
    from omnigent.harnesses.claude_native.main import _websocket_connect

    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "caller")
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test")
    mount = f"{scheme}://127.0.0.1/api/2.0/omnigent"
    set_session_host("attached_native", "owner")
    headers = {OMNIGENT_SLICE_KEY_HEADER: "caller", "Authorization": "Bearer test"}

    def handle(request: httpx.Request) -> httpx.Response:
        expected = "owner" if "/attached_native/" in request.url.path else "caller"
        assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == expected
        assert request.headers["Authorization"] == "Bearer test"
        return httpx.Response(200, json={})

    def connect(url, **kwargs):
        assert kwargs["additional_headers"][OMNIGENT_SLICE_KEY_HEADER] == "owner"
        assert kwargs["additional_headers"]["Authorization"] == "Bearer test"

    monkeypatch.setattr("websockets.connect", connect)
    try:
        async with open_server_client(
            mount,
            headers=headers,
            host_id="caller",
            auth=_DatabricksTokenAuth(server_url=mount),
            transport=httpx.MockTransport(handle),
        ) as client:
            await client.get("/v1/sessions/attached_native/resources")
            await client.get("/v1/hosts/caller")
            await client.get("/v1/sessions/unmapped_native/resources")
        _websocket_connect(
            mount.replace("http", "ws", 1) + "/v1/sessions/attached_native/resources/t/attach",
            headers=headers,
        )
        assert headers[OMNIGENT_SLICE_KEY_HEADER] == "caller"
    finally:
        set_session_host("attached_native", None)


def test_set_session_host_clears_on_empty() -> None:
    """A ``None``/empty host clears any stale mapping."""
    set_session_host("conv_1", "host_abc")
    assert get_session_host("conv_1") == "host_abc"
    set_session_host("conv_1", None)
    assert get_session_host("conv_1") is None
    set_session_host("conv_1", "host_abc")
    set_session_host("conv_1", "")
    assert get_session_host("conv_1") is None


def _slice_key(auth: _DatabricksTokenAuth, url: str) -> str | None:
    """Run *auth*'s flow on a GET to *url* and return the slice-key header."""
    flow = auth.auth_flow(httpx.Request("GET", url))
    request = next(flow)
    flow.close()
    return request.headers.get(OMNIGENT_SLICE_KEY_HEADER)


def test_auth_sends_the_default_key_on_every_route() -> None:
    """The auth keys no request by session; the SDK client's routing transport does."""
    mount = "https://acme.databricks.com/api/2.0/omnigent"
    set_session_host(OLD_SESSION, "host_abc")
    auth = _DatabricksTokenAuth(server_url=mount)
    for route in (f"sessions/{OLD_SESSION}/events", "agents", "hosts", "sessions", "me"):
        assert _slice_key(auth, f"{mount}/v1/{route}") == "client_key"


def test_auth_no_key_off_workspace() -> None:
    """An unsharded server has no sharding layer, so no key is sent."""
    set_session_host(OLD_SESSION, "host_abc")
    auth = _DatabricksTokenAuth(server_url="http://127.0.0.1:6767")
    assert _slice_key(auth, f"http://127.0.0.1:6767/v1/sessions/{OLD_SESSION}/events") is None


@pytest.mark.parametrize("target_host", ["host_new", None], ids=["known-host", "unknown-host"])
@pytest.mark.asyncio
async def test_sdk_client_keys_each_session_by_its_own_host(
    monkeypatch: pytest.MonkeyPatch, target_host: str | None
) -> None:
    """A session request names its own host, else the default key, as other routes do."""
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "host_fallback")
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda _url: "1234")
    mount = "http://127.0.0.1/api/2.0/omnigent"
    set_session_host(OLD_SESSION, "host_old")
    set_session_host(NEW_SESSION, target_host)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Databricks-Org-Id"] == "1234"
        route = request.url.path.removeprefix("/api/2.0/omnigent/v1/")
        if route in {"agents", "hosts", "runners"}:
            assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == "host_fallback"
            return httpx.Response(200, json={"data": [], "hosts": []})
        assert route == f"sessions/{NEW_SESSION}"
        assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == (target_host or "host_fallback")
        return httpx.Response(
            200, json={"id": NEW_SESSION, "agent_id": "ag_new", "status": "idle", "created_at": 0}
        )

    async with open_sdk_client(
        mount, auth=_DatabricksTokenAuth(server_url=mount), transport=httpx.MockTransport(handler)
    ) as client:
        assert (await client.sessions.get(NEW_SESSION)).id == NEW_SESSION
        assert await client.sessions.list_registered_agents() == []
        assert await client.sessions.list_launch_hosts() == []
        assert await client.sessions.resolve_online_runner() is None
        assert OMNIGENT_SLICE_KEY_HEADER not in client._http.headers


@pytest.mark.parametrize(
    ("refusal", "expected_keys"),
    [
        # Named: retried on that host once, and later requests go there directly.
        ({"slice_key": "host_owner"}, ["client_key", "host_owner", "host_owner"]),
        # Unnamed: the host is looked up, then the request is retried on it.
        ({}, ["client_key", "lookup", "host_owner", "host_owner"]),
        # Null: one keyless resend per request, nothing cached, no further retries.
        ({"slice_key": None}, ["client_key", None, "client_key", None]),
    ],
    ids=["named", "unnamed", "keyless"],
)
@pytest.mark.asyncio
async def test_sdk_client_recovers_once_from_a_wrong_replica_refusal(
    monkeypatch: pytest.MonkeyPatch, refusal: dict[str, str | None], expected_keys: list
) -> None:
    """The REPL's client follows each refusal form once, after the CLI auth keyed the request."""
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda _url: None)
    mount = "https://workspace.example.com/api/2.0/omnigent"
    owner = refusal.get("slice_key", "host_owner")
    keys: list[str | None] = []
    session = {"id": NEW_SESSION, "agent_id": "ag", "status": "idle", "created_at": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        if "include_liveness" in request.url.params:
            keys.append("lookup")
            return httpx.Response(200, json={**session, "host_id": "host_owner"})
        keys.append(request.headers.get(OMNIGENT_SLICE_KEY_HEADER))
        if keys[-1] != owner:
            return httpx.Response(400, json={"error": {"code": "wrong_replica", **refusal}})
        return httpx.Response(200, json=session)

    async with open_sdk_client(
        mount, auth=_DatabricksTokenAuth(server_url=mount), transport=httpx.MockTransport(handler)
    ) as client:
        assert (await client.sessions.get(NEW_SESSION)).id == NEW_SESSION
        assert (await client.sessions.get(NEW_SESSION)).id == NEW_SESSION

    assert keys == expected_keys
    assert get_session_host(NEW_SESSION) == (owner and "host_owner")


def test_session_host_cache_shares_one_entry_across_id_spellings() -> None:
    """A host recorded under a dashed or legacy-prefixed id routes the canonical id too."""
    dashed = str(uuid.UUID(NEW_SESSION))
    set_session_host(dashed, "host_abc")
    assert get_session_host(NEW_SESSION) == "host_abc"
    assert get_session_host(f"conv_{NEW_SESSION}") == "host_abc"
    set_session_host(f"conv_{NEW_SESSION}", None)
    assert get_session_host(dashed) is None


_WRONG_REPLICA = {"error": {"code": "wrong_replica"}}


@pytest.mark.parametrize(
    ("replies", "expected_keys", "status"),
    [
        ([_WRONG_REPLICA, None], ["client_key", None], 200),
        ([{"error": {"code": "invalid_argument"}}], ["client_key"], 400),
        ([_WRONG_REPLICA, _WRONG_REPLICA, None], ["client_key", None], 400),
    ],
    ids=["recovers", "other-400", "single-replay"],
)
def test_sync_client_resends_only_a_wrong_replica_miss_once(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[dict[str, object] | None],
    expected_keys: list[str | None],
    status: int,
) -> None:
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda _url: None)
    mount = "https://workspace.example.com/api/2.0/omnigent"
    keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get(OMNIGENT_SLICE_KEY_HEADER))
        reply = replies[len(keys) - 1]
        return httpx.Response(200, json={}) if reply is None else httpx.Response(400, json=reply)

    auth = _DatabricksTokenAuth(server_url=mount)
    with httpx.Client(auth=auth, transport=httpx.MockTransport(handler)) as client:
        assert client.get(f"{mount}/v1/sessions/conv_unknown").status_code == status

    assert keys == expected_keys
