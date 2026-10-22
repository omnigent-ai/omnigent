"""Session host affinity and its integration with CLI authentication."""

from __future__ import annotations

import httpx
import pytest
from omnigent_client import OmnigentClient

from omnigent.chat import _DatabricksTokenAuth
from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER
from omnigent.session_host import get_session_host, route_session_request, set_session_host


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


def test_auth_keys_by_session_host_on_workspace_mount() -> None:
    """The auth pins its session's requests to that session's host replica.

    The client is built for one session; it names the session (not a parsed
    URL), and the auth resolves the host from the session→host map. On the
    workspace mount the routing header rides; an unmapped session sends none.
    """
    mount = "https://acme.databricks.com/api/2.0/omnigent"
    set_session_host("conv_1", "host_abc")

    keyed = _DatabricksTokenAuth(server_url=mount, session_id="conv_1")
    assert _slice_key(keyed, f"{mount}/v1/sessions/conv_1/events") == "host_abc"
    assert _slice_key(keyed, f"{mount}/v1/sessions/conv_1/stream") == "host_abc"
    for route in ("agents", "hosts", "runners", "sessions", "sessions-other", "me"):
        assert _slice_key(keyed, f"{mount}/v1/{route}") is None

    # Unmapped session → no key (workspace fallback).
    unmapped = _DatabricksTokenAuth(server_url=mount, session_id="conv_unknown")
    assert _slice_key(unmapped, f"{mount}/v1/sessions/conv_unknown/events") is None

    # Hostless (local / no session) client → no key.
    hostless = _DatabricksTokenAuth(server_url=mount, session_id=None)
    assert _slice_key(hostless, f"{mount}/v1/sessions/conv_1/events") is None


def test_auth_no_key_off_workspace() -> None:
    """An unsharded server has no sharding layer, so no key is sent."""
    set_session_host("conv_1", "host_abc")
    auth = _DatabricksTokenAuth(server_url="http://127.0.0.1:6767", session_id="conv_1")
    assert _slice_key(auth, "http://127.0.0.1:6767/v1/sessions/conv_1/events") is None


@pytest.mark.parametrize("target_host", ["host_new", None], ids=["override", "fallback"])
@pytest.mark.asyncio
async def test_request_routing_follows_the_target_session(
    monkeypatch: pytest.MonkeyPatch, target_host: str | None
) -> None:
    """The SDK hook overrides stale auth routing without changing shared headers."""
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "host_fallback")
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda _url: "1234")
    mount = "http://127.0.0.1/api/2.0/omnigent"
    set_session_host("conv_old", "host_old")
    set_session_host("conv_new", target_host)
    auth = _DatabricksTokenAuth(server_url=mount, session_id="conv_old")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Databricks-Org-Id"] == "1234"
        route = request.url.path.removeprefix("/api/2.0/omnigent/v1/")
        if route in {"agents", "hosts", "runners"}:
            assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == "host_fallback"
            return httpx.Response(200, json={"data": [], "hosts": []})
        assert route == "sessions/conv_new"
        assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == (target_host or "host_fallback")
        return httpx.Response(
            200, json={"id": "conv_new", "agent_id": "ag_new", "status": "idle", "created_at": 0}
        )

    async with OmnigentClient(mount, auth=auth, request_hooks=[route_session_request]) as client:
        async with httpx.MockTransport(handler) as transport:
            with monkeypatch.context() as patcher:
                patcher.setattr(client._http, "_transport", transport)
                assert (await client.sessions.get("conv_new")).id == "conv_new"
                assert await client.sessions.list_registered_agents() == []
                assert await client.sessions.list_launch_hosts() == []
                assert await client.sessions.resolve_online_runner() is None
                assert OMNIGENT_SLICE_KEY_HEADER not in client._http.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "content_type", "expected_host"),
    [
        ("/v1/hosts", b"", "", "host_existing"),
        ("/v1/sessions-other", b"", "", "host_existing"),
        ("/v1/sessions/conv%2Fencoded/events?q=1", b"", "", "host_encoded"),
        ("/v1/sessions", b'{"host_id":"host_launch"}', "application/json", "host_launch"),
        ("/v1/sessions", b"[]", "application/json", "host_fallback"),
        ("/v1/sessions", b"broken", "application/json", "host_fallback"),
        ("/v1/sessions", b"bundle", "multipart/form-data", "host_fallback"),
    ],
)
async def test_request_routing_only_interprets_session_routes(
    monkeypatch: pytest.MonkeyPatch, path: str, body: bytes, content_type: str, expected_host: str
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "host_fallback")
    set_session_host("conv/encoded", "host_encoded")
    request = httpx.Request(
        "POST",
        f"http://127.0.0.1/api/2.0/omnigent{path}",
        content=body,
        headers={OMNIGENT_SLICE_KEY_HEADER: "host_existing", "content-type": content_type},
    )
    await route_session_request(request)
    assert request.headers[OMNIGENT_SLICE_KEY_HEADER] == expected_host
    assert request.content == body
