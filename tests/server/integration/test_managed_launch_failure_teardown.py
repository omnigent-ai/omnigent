"""Managed-sandbox teardown when the runner launch fails after the host is online.

A ``host_type="managed"`` session whose launch fails AFTER the sandbox
host has registered must not leave the sandbox running. Two failure
branches of the post-online launch stage are exercised:

- the host refuses the runner launch (harness not configured), and
- the launched runner never connects its tunnel within the connect
  grace.

In both cases the launch settles as ``failed`` on the session snapshot.
The provider-side terminate must then fire and the managed host row
(which holds the sandbox's launch token) must be deleted — the same
teardown the delete-during-provisioning branch already performs. A
regression here leaks a running, credential-bearing sandbox whose only
reaper is the provider's lifetime cap.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runner.identity import token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import (
    FakeSandboxLauncher,
    HostStartInvocation,
    create_test_agent,
    install_fake_modal_launcher,
)

pytestmark = pytest.mark.asyncio


def _websocket_scope(path: str) -> dict[str, object]:
    """Build an ASGI WebSocket scope.

    :param path: WebSocket path.
    :returns: Minimal ASGI WebSocket scope.
    """
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


@dataclass
class _Env:
    """Assembled managed-session test environment.

    :param app: The full FastAPI app under test.
    :param client: HTTP client bound to *app*.
    :param host_store: The app's host store (also holds the managed
        credential/sandbox columns).
    :param conv_store: The app's conversation store.
    """

    app: FastAPI
    client: AsyncClient
    host_store: HostStore
    conv_store: SqlAlchemyConversationStore


@pytest_asyncio.fixture()
async def teardown_env(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> AsyncIterator[_Env]:
    """Full app wired for managed-host sessions (no real sandbox).

    Builds the production ``create_app`` with host + managed-host
    stores and a modal ``sandbox:`` config, so a ``host_type="managed"``
    create exercises the real route, tunnel, and store paths end to
    end. Only the sandbox itself is fake.

    :param runtime_init: Ensures runtime singletons are initialized.
    :param db_uri: SQLite URI shared by every store in the app.
    :param tmp_path: Per-test scratch dir for artifact/cache stores.
    :returns: The assembled environment.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conv_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=host_store,
        sandbox_config=parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://managed-test.example.com",
                "modal": {"image": "docker.io/test/omnigent-host:latest"},
            }
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield _Env(app=app, client=client, host_store=host_store, conv_store=conv_store)


async def _sandbox_host(
    app: FastAPI,
    host_id: str,
    host_name: str,
    token: str,
    *,
    refuse_launch: bool,
) -> ApplicationCommunicator:
    """Act as the host process inside the (fake) sandbox.

    Connects to the app's real host tunnel authenticating only with
    the launch token (exactly what a sandbox has), sends hello with
    the server-injected host name, then answers the launch frame —
    refusing it as harness-not-configured when *refuse_launch* is
    set, confirming a launched runner (that never connects) otherwise.

    :param app: The app whose tunnel to dial.
    :param host_id: Server-chosen host identity from the launch env.
    :param host_name: Server-chosen host name from the launch env.
    :param token: Raw launch token from the launch env.
    :param refuse_launch: Answer the launch frame with a structured
        harness-not-configured refusal instead of a launched result.
    :returns: The live tunnel communicator. The CALLER must keep it
        referenced for as long as the host should stay online.
    """
    scope = _websocket_scope(f"/v1/hosts/{host_id}/tunnel")
    scope["headers"] = [(b"x-omnigent-host-token", token.encode("ascii"))]
    comm = ApplicationCommunicator(app, scope)
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=5.0)
    assert accepted["type"] == "websocket.accept", f"tunnel refused: {accepted!r}"
    hello = encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=host_name,
            runners=[],
        )
    )
    await comm.send_input({"type": "websocket.receive", "text": hello})
    # Serve frames until the launch request arrives, then answer it.
    for _ in range(50):
        output = await comm.receive_output(timeout=10.0)
        if output["type"] != "websocket.send":
            continue
        try:
            frame = decode_host_frame(output["text"])
        except ValueError:
            # Runner-encoded ping frames share the socket; skip them.
            continue
        if isinstance(frame, HostLaunchRunnerFrame):
            if refuse_launch:
                result = HostLaunchRunnerResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error="harness is not configured on the sandbox host",
                    error_code=HARNESS_NOT_CONFIGURED_ERROR_CODE,
                )
            else:
                result = HostLaunchRunnerResultFrame(
                    request_id=frame.request_id,
                    status="launched",
                    runner_id=token_bound_runner_id(frame.binding_token),
                )
            await comm.send_input(
                {"type": "websocket.receive", "text": encode_host_frame(result)},
            )
            return comm
    raise AssertionError("fake sandbox host never received a launch frame")


async def _wait_for_failed_launch(
    env: _Env,
    session_id: str,
    *,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    """Poll the session snapshot until the launch settles as failed.

    The same observation a client makes via ``GET /v1/sessions/{id}``:
    a failed managed launch is retained on the snapshot's
    ``sandbox_status`` field.

    :param env: The managed-session test environment.
    :param session_id: The created session's id.
    :param timeout_s: Poll budget.
    :returns: The failed ``sandbox_status`` payload.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        snapshot = await env.client.get(f"/v1/sessions/{session_id}")
        assert snapshot.status_code == 200, snapshot.text
        status = snapshot.json()["sandbox_status"]
        if status is not None and status["stage"] == "failed":
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"managed launch for session {session_id} never settled as failed")


async def _wait_for_sandbox_teardown(
    env: _Env,
    fake: FakeSandboxLauncher,
    host_id: str,
    *,
    timeout_s: float = 5.0,
) -> None:
    """Wait for the failed launch's sandbox teardown to land.

    Teardown means both halves of ``terminate_managed_host``: the
    provider-side terminate fired for the sandbox, and the managed
    host row (whose existence keeps the launch token resolvable) is
    deleted. The launch-settled status precedes the teardown by a
    scheduling beat, so poll rather than assert immediately.

    :param env: The managed-session test environment.
    :param fake: The fake launcher recording terminations.
    :param host_id: The managed host bound to the failed session.
    :param timeout_s: Poll budget before declaring the sandbox leaked.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if fake.terminated and env.host_store.get_host(host_id) is None:
            return
        await asyncio.sleep(0.05)
    host = env.host_store.get_host(host_id)
    raise AssertionError(
        "managed sandbox leaked after the launch settled as failed: "
        f"provider terminations={fake.terminated!r} (expected the failed "
        "launch to terminate the sandbox), host row="
        f"{'present — launch token still resolvable' if host is not None else 'deleted'}"
    )


async def test_harness_refusal_failure_tears_down_managed_sandbox(
    teardown_env: _Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A managed launch the host refuses (harness not configured) settles
    as ``failed`` — and must tear its sandbox down.

    The sandbox host registers over the real tunnel, so the sandbox is
    live and holds its launch token when the refusal lands. Before the
    teardown existed, this branch only published the failed status and
    returned: the sandbox kept running with no code path left to ever
    delete it.
    """
    env = teardown_env
    # A healthy fake host registers in well under a second; shrink the
    # online-poll budget so a registration regression fails in seconds.
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []
    launched_host_ids: list[str] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the refusing fake sandbox host when the launcher 'starts' it."""
        launched_host_ids.append(invocation.host_id)
        future = asyncio.run_coroutine_threadsafe(
            _sandbox_host(
                env.app,
                invocation.host_id,
                invocation.host_name,
                invocation.token,
                refuse_launch=True,
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-refusal-teardown-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    # The host's structured refusal settles the launch as failed, and
    # the reason reaches the snapshot a reloading client cold-loads.
    status = await _wait_for_failed_launch(env, session_id)
    assert "not configured" in (status["error"] or ""), status

    # Keep the fake host's tunnel alive (referenced) through the
    # teardown assertions — the sandbox is genuinely live when the
    # refusal lands, exactly the leaked state.
    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1

    # The session row survives (the operator keeps the transcript of
    # why the launch failed). The host binding is deliberately NOT read
    # off the conversation row here: deleting the host row (the
    # teardown under test) also clears the session's binding to it, so
    # the sandbox's host id comes from the launch invocation instead.
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None, "failed launch must not delete the session row"
    assert len(launched_host_ids) == 1, "exactly one sandbox launch expected"

    # A launch that settles as failed leaves no running sandbox: the
    # provider terminate fires and the host row (launch token) goes.
    await _wait_for_sandbox_teardown(env, fake, launched_host_ids[0])
    assert fake.terminated == ["sb-fake-1"]
    del tunnels


async def test_runner_connect_timeout_failure_tears_down_managed_sandbox(
    teardown_env: _Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A managed launch whose runner never connects settles as ``failed``
    after the connect grace — and must tear its sandbox down.

    The host confirms the launch but no runner tunnel ever arrives
    (a cold or overloaded node does this on its own). Before the
    teardown existed, the connect timeout only published the failed
    status and returned: the sandbox kept running with no code path
    left to ever delete it.
    """
    env = teardown_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    # No runner ever connects in this test by design; shrink the
    # connect grace so the launch settles in milliseconds, not 30s.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []
    launched_host_ids: list[str] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the confirming fake sandbox host when the launcher 'starts' it."""
        launched_host_ids.append(invocation.host_id)
        future = asyncio.run_coroutine_threadsafe(
            _sandbox_host(
                env.app,
                invocation.host_id,
                invocation.host_name,
                invocation.token,
                refuse_launch=False,
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-connect-timeout-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    # The connect grace expires and the launch settles as failed with
    # the connect-timeout reason on the snapshot.
    status = await _wait_for_failed_launch(env, session_id)
    assert "did not connect" in (status["error"] or ""), status

    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1

    # Same as the refusal test: the sandbox's host id comes from the
    # launch invocation, because a correct teardown clears the
    # conversation's host binding along with the host row.
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None, "failed launch must not delete the session row"
    assert len(launched_host_ids) == 1, "exactly one sandbox launch expected"

    # A launch that settles as failed leaves no running sandbox: the
    # provider terminate fires and the host row (launch token) goes.
    await _wait_for_sandbox_teardown(env, fake, launched_host_ids[0])
    assert fake.terminated == ["sb-fake-1"]
    del tunnels
