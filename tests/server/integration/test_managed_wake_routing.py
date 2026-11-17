"""A client request must not wake a managed host on a replica its tunnel will not dial.

Each case drives a real HTTP route through the two-replica stack against a dormant,
resumable managed host. The wake's collaborators are faked and append to one ordered
list, so a test can assert both that a wake was kicked and where it ended.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER
from omnigent.host.frames import HostHelloFrame
from omnigent.server import managed_hosts
from omnigent.server.managed_hosts import REQUESTED_HOST_ID_LABEL_KEY
from omnigent.server.routes import _host_launch
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.common import (
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
    _COMPACT_TYPE,
)
from omnigent.stores.host_store import HostStore
from tests.server.helpers import build_agent_bundle
from tests.server.integration.test_sessions_subagent_context import (
    two_replica_stack as two_replica_stack,
)

HOST_ID = "9e4b1c7a2d3f4e5a8b6c0d1e2f3a4b5c"
FOREIGN_KEY = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
OWNER = "alice"
MESSAGE = {
    "type": "message",
    "data": {"role": "user", "content": [{"type": "input_text", "text": "wake up"}]},
}
TERMINAL = {"terminal": "zsh", "session_key": "shell-1"}
TERMINALS = {"zsh": {"command": "zsh", "os_env": {"type": "caller_process", "cwd": "."}}}


async def _create_session(client: Any) -> str:
    """Create a session on an agent that declares a terminal, so terminal routes accept it."""
    bundle = build_agent_bundle(name="wake-agent", terminals=TERMINALS)
    response = await client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"X-Forwarded-Email": OWNER},
    )
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _headers(slice_key: str | None) -> dict[str, str]:
    headers = {"X-Forwarded-Email": OWNER}
    if slice_key is not None:
        headers[OMNIGENT_SLICE_KEY_HEADER] = slice_key
    return headers


def _route(entry_point: str, session_id: str) -> tuple[str, str, dict[str, Any] | None]:
    """The method, path, and body that reach the wake through *entry_point*."""
    if entry_point == "message":
        return "POST", f"/v1/sessions/{session_id}/events", MESSAGE
    if entry_point == "compact":
        return "POST", f"/v1/sessions/{session_id}/events", {"type": _COMPACT_TYPE}
    return "POST", f"/v1/sessions/{session_id}/resources/terminals", TERMINAL


@pytest.fixture
def managed_wake(
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
    two_replica_stack: Any,
) -> Callable[..., Any]:
    """Build a dormant managed host and session; return an arranging helper.

    The woken host dials ``dial_replica`` (the owner for a keyed host, the
    default route for an unkeyed one) and records ``resume`` then ``launch``
    then ``ready`` in ``events``.
    """
    stack = two_replica_stack
    events: list[str] = []
    dial: dict[str, Any] = {"replica": stack.tunnel_replica}

    async def fake_resume(host_id: str, *_args: object, **_kwargs: object) -> None:
        events.append("resume")
        dial["replica"].state.host_registry.register(
            host_id,
            AsyncMock(),
            HostHelloFrame(version="test", frame_protocol_version=1, name="wake", runners=[]),
            owner=None,
            registered_with_managed_token=True,
        )

    async def fake_launch(*_args: object, **_kwargs: object) -> object:
        events.append("launch")
        return sessions_module._HostLaunchAttempt(runner_id="runner_woken")

    async def fake_wait(*_args: object, **_kwargs: object) -> object:
        return object()

    def fake_publish(_session_id: str, stage: str, *_args: object, **_kwargs: object) -> None:
        events.append(stage)

    monkeypatch.setattr(managed_hosts, "resume_managed_host", fake_resume)
    monkeypatch.setattr(orchestration, "host_resume_supported", lambda _host, _config: True)
    monkeypatch.setattr(orchestration, "host_sandbox_is_running", lambda _host, _config: None)
    monkeypatch.setattr(sessions_module, "_launch_runner_on_host", fake_launch)
    monkeypatch.setattr(sessions_module, "_publish_sandbox_status", fake_publish)
    for replica in (stack.tunnel_replica, stack.keyless_replica):
        monkeypatch.setattr(replica.state.tunnel_registry, "wait_for_runner", fake_wait)
    monkeypatch.setattr(_host_launch, "_host_sharded", True)

    async def arrange(
        *,
        keyed: bool,
        wrapper: bool = False,
        dial_replica: str = "tunnel",
        sharded: bool = True,
        local_tunnel_on_keyless: bool = False,
    ) -> str:
        monkeypatch.setattr(_host_launch, "_host_sharded", sharded)
        dial["replica"] = (
            stack.tunnel_replica if dial_replica == "tunnel" else stack.keyless_replica
        )
        events.clear()
        HostStore(db_uri).register_managed_host(
            host_id=HOST_ID,
            name="managed-wake-host",
            user_id=OWNER,
            token="tok-managed-wake",
            provider="modal",
            sandbox_id="sb-managed-wake",
            token_expires_at=9_999_999_999,
        )
        session_id = await _create_session(stack.keyless_client)
        stack.conv_store.set_host_id(session_id, HOST_ID, workspace="/worker")
        labels: dict[str, str] = {}
        if keyed:
            labels[REQUESTED_HOST_ID_LABEL_KEY] = HOST_ID
        if wrapper:
            labels[_CLAUDE_NATIVE_WRAPPER_LABEL_KEY] = _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE
        stack.conv_store.set_labels(session_id, labels)
        if local_tunnel_on_keyless:
            # A fresh frame keeps the tunnel from reading as stale and being torn down.
            connection = stack.keyless_replica.state.host_registry.register(
                HOST_ID,
                AsyncMock(),
                HostHelloFrame(version="test", frame_protocol_version=1, name="local", runners=[]),
                owner=None,
                registered_with_managed_token=True,
            )
            connection.last_frame_at = time.time()
        return session_id

    return arrange, events  # type: ignore[return-value]


def _wrong_replica(response: Any) -> bool:
    return response.status_code == 400 and response.json()["error"]["code"] == "wrong_replica"


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
@pytest.mark.parametrize("entry_point", ["message", "compact", "terminal"])
async def test_keyed_host_mismatched_key_is_rerouted_before_any_wake(
    two_replica_stack: Any, managed_wake: Any, entry_point: str
) -> None:
    arrange, events = managed_wake
    session_id = await arrange(keyed=True, wrapper=entry_point == "compact")
    method, path, body = _route(entry_point, session_id)

    response = await two_replica_stack.keyless_client.request(
        method, path, headers=_headers(FOREIGN_KEY), json=body
    )

    assert _wrong_replica(response), response.text
    # Keyed: the body names the host's key so the client resends with it.
    assert response.json()["error"]["slice_key"] == HOST_ID
    assert events == []


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
@pytest.mark.parametrize("entry_point", ["message", "terminal"])
async def test_unkeyed_host_keyed_request_gets_plain_wrong_replica(
    two_replica_stack: Any, managed_wake: Any, entry_point: str
) -> None:
    arrange, events = managed_wake
    session_id = await arrange(keyed=False)
    method, path, body = _route(entry_point, session_id)

    response = await two_replica_stack.keyless_client.request(
        method, path, headers=_headers(FOREIGN_KEY), json=body
    )

    assert _wrong_replica(response), response.text
    # Unkeyed: there is no key to name, so the body must not point at the host.
    assert "slice_key" not in response.json()["error"]
    assert events == []


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
@pytest.mark.parametrize("entry_point", ["message", "compact", "terminal"])
async def test_matching_key_wakes_and_launches_runner_before_ready(
    two_replica_stack: Any, managed_wake: Any, entry_point: str
) -> None:
    arrange, events = managed_wake
    session_id = await arrange(keyed=True, wrapper=entry_point == "compact")
    method, path, body = _route(entry_point, session_id)

    response = await two_replica_stack.tunnel_client.request(
        method, path, headers=_headers(HOST_ID), json=body
    )

    assert not _wrong_replica(response), response.text
    assert "resume" in events and "launch" in events and "ready" in events, events
    assert events.index("resume") < events.index("launch") < events.index("ready"), events


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
async def test_unkeyed_host_wakes_from_the_default_route(
    two_replica_stack: Any, managed_wake: Any
) -> None:
    arrange, events = managed_wake
    session_id = await arrange(keyed=False, dial_replica="keyless")

    response = await two_replica_stack.keyless_client.post(
        f"/v1/sessions/{session_id}/events", headers=_headers(None), json=MESSAGE
    )

    assert not _wrong_replica(response), response.text
    assert events.index("resume") < events.index("launch") < events.index("ready"), events


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
async def test_single_replica_is_unaffected_by_a_foreign_key(
    two_replica_stack: Any, managed_wake: Any
) -> None:
    arrange, events = managed_wake
    session_id = await arrange(keyed=True, dial_replica="keyless", sharded=False)

    response = await two_replica_stack.keyless_client.post(
        f"/v1/sessions/{session_id}/events", headers=_headers(FOREIGN_KEY), json=MESSAGE
    )

    assert not _wrong_replica(response), response.text
    assert events.index("resume") < events.index("launch") < events.index("ready"), events


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
async def test_live_local_tunnel_bypasses_the_guard(
    two_replica_stack: Any, managed_wake: Any
) -> None:
    arrange, events = managed_wake
    # An unkeyed host dials the default route, so its live tunnel can sit on this replica.
    session_id = await arrange(keyed=False, dial_replica="keyless", local_tunnel_on_keyless=True)

    response = await two_replica_stack.keyless_client.post(
        f"/v1/sessions/{session_id}/events", headers=_headers(FOREIGN_KEY), json=MESSAGE
    )

    assert not _wrong_replica(response), response.text
    # The local tunnel passes the guard, so the wake continues here as it did before.
    assert events.index("resume") < events.index("launch") < events.index("ready"), events


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
async def test_non_resumable_relaunch_is_rerouted_before_the_kick(
    two_replica_stack: Any, managed_wake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-resumable host reaches the guard inside the relaunch helper."""
    from omnigent.server.routes import sessions as sessions_module

    arrange, events = managed_wake
    # Non-resumable hosts skip the stale-wake check, so only the relaunch guard runs.
    monkeypatch.setattr(orchestration, "host_resume_supported", lambda _host, _config: False)

    def record_relaunch(**_kwargs: object) -> None:
        events.append("relaunch")

    monkeypatch.setattr(orchestration, "_kick_managed_relaunch", record_relaunch)
    monkeypatch.setattr(sessions_module, "_kick_managed_relaunch", record_relaunch)
    session_id = await arrange(keyed=True)

    response = await two_replica_stack.keyless_client.post(
        f"/v1/sessions/{session_id}/events", headers=_headers(FOREIGN_KEY), json=MESSAGE
    )

    assert _wrong_replica(response), response.text
    assert response.json()["error"]["slice_key"] == HOST_ID
    assert events == [], events


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
async def test_server_internal_wake_is_not_guarded(
    two_replica_stack: Any, managed_wake: Any
) -> None:
    arrange, events = managed_wake
    session_id = await arrange(keyed=True, dial_replica="keyless")
    conv = two_replica_stack.conv_store.get_conversation(session_id)
    assert conv is not None

    await orchestration.ensure_runner_connected(
        session_id=session_id,
        conv=conv,
        app_state=two_replica_stack.keyless_replica.state,
        conversation_store=two_replica_stack.conv_store,
        runner_router=None,
    )

    # No request means no routing key to check, so the wake runs here as before.
    assert "resume" in events, events
