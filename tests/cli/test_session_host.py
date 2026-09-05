"""Unit tests for client-side session→host routing helpers in ``chat``."""

from __future__ import annotations

import httpx
import pytest

from omnigent.chat import (
    _DatabricksTokenAuth,
    _server_auth,
    get_session_host,
    set_session_host,
)
from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER


@pytest.fixture(autouse=True)
def _clear_map() -> None:
    """Reset the module-global session→host map between tests."""
    from omnigent import chat

    chat._session_hosts.clear()
    yield
    chat._session_hosts.clear()


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
    # Any request on this client rides the same key (it is single-session).
    assert _slice_key(keyed, f"{mount}/v1/sessions/conv_1/stream") == "host_abc"

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


def test_auth_suppresses_only_the_proven_stale_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery can omit one stale key without disabling later host routing."""
    mount = "https://acme.databricks.com/api/2.0/omnigent"
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    set_session_host("conv_1", "host_stale")

    auth = _server_auth(
        server_url=mount,
        session_id="conv_1",
        suppress_slice_key_for_host="host_stale",
    )
    assert isinstance(auth, _DatabricksTokenAuth)
    assert _slice_key(auth, f"{mount}/v1/sessions/conv_1/events") is None

    set_session_host("conv_1", "host_new")
    assert _slice_key(auth, f"{mount}/v1/sessions/conv_1/events") == "host_new"


def test_auth_promotes_suppressed_host_when_keyless_route_becomes_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect can restore keyed routing without restarting the REPL."""
    mount = "https://acme.databricks.com/api/2.0/omnigent"
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    set_session_host("conv_1", "host_stale")
    auth = _DatabricksTokenAuth(
        server_url=mount,
        session_id="conv_1",
        suppress_slice_key_for_host="host_stale",
    )
    flow = auth.auth_flow(httpx.Request("POST", f"{mount}/v1/sessions/conv_1/events"))

    first = next(flow)
    assert first.headers.get(OMNIGENT_SLICE_KEY_HEADER) is None
    second = flow.send(
        httpx.Response(
            400,
            json={"error": {"code": "wrong_replica"}},
            request=first,
        )
    )
    assert second.headers[OMNIGENT_SLICE_KEY_HEADER] == "host_stale"
    with pytest.raises(StopIteration):
        flow.send(httpx.Response(200, json={"ok": True}, request=second))

    assert _slice_key(auth, f"{mount}/v1/sessions/conv_1/events") == "host_stale"


def test_auth_concurrent_keyless_requests_both_retry_keyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One promotion does not make another in-flight request skip its retry."""
    mount = "https://acme.databricks.com/api/2.0/omnigent"
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    set_session_host("conv_1", "host_stale")
    auth = _DatabricksTokenAuth(
        server_url=mount,
        session_id="conv_1",
        suppress_slice_key_for_host="host_stale",
    )
    flows = [
        auth.auth_flow(httpx.Request("POST", f"{mount}/v1/sessions/conv_1/events"))
        for _ in range(2)
    ]
    first_requests = [next(flow) for flow in flows]
    assert all(
        request.headers.get(OMNIGENT_SLICE_KEY_HEADER) is None for request in first_requests
    )

    retry_requests = [
        flow.send(
            httpx.Response(
                400,
                json={"error": {"code": "wrong_replica"}},
                request=request,
            )
        )
        for flow, request in zip(flows, first_requests, strict=True)
    ]

    assert all(
        request.headers[OMNIGENT_SLICE_KEY_HEADER] == "host_stale" for request in retry_requests
    )
    for flow, request in zip(flows, retry_requests, strict=True):
        with pytest.raises(StopIteration):
            flow.send(httpx.Response(200, json={"ok": True}, request=request))


def test_auth_failed_concurrent_keyed_retries_do_not_commit_keyless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed speculative retries do not become another flow's baseline."""
    mount = "https://acme.databricks.com/api/2.0/omnigent"
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "test-token")
    set_session_host("conv_1", "host_current")
    auth = _DatabricksTokenAuth(server_url=mount, session_id="conv_1")
    flows = [
        auth.auth_flow(httpx.Request("POST", f"{mount}/v1/sessions/conv_1/events"))
        for _ in range(2)
    ]
    first_requests = [next(flow) for flow in flows]
    assert all(
        request.headers[OMNIGENT_SLICE_KEY_HEADER] == "host_current" for request in first_requests
    )

    retry_requests = [
        flow.send(
            httpx.Response(
                400,
                json={"error": {"code": "wrong_replica"}},
                request=request,
            )
        )
        for flow, request in zip(flows, first_requests, strict=True)
    ]
    assert all(
        request.headers.get(OMNIGENT_SLICE_KEY_HEADER) is None for request in retry_requests
    )

    for flow, request in zip(flows, retry_requests, strict=True):
        with pytest.raises(StopIteration):
            flow.send(
                httpx.Response(
                    400,
                    json={"error": {"code": "wrong_replica"}},
                    request=request,
                )
            )

    assert _slice_key(auth, f"{mount}/v1/sessions/conv_1/events") == "host_current"
