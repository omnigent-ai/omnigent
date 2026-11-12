"""Integration tests for session creation with host_id and reconnect reconciliation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import pytest_asyncio
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.onboarding.sandboxes.base import render_host_config_write_command
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.host_registry import HostRegistry
from omnigent.server.managed_hosts import (
    ManagedHostLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    parse_sandbox_config,
)
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.server.sharing_settings import SiblingSessionDestinations
from omnigent.server.startup_inputs import STARTUP_LABEL, STARTUP_WITHDRAWN
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests._helpers.polling import poll_until_async
from tests.server.helpers import (
    FakeSandboxLauncher,
    HostStartInvocation,
    build_agent_bundle,
    create_test_agent,
    install_fake_modal_launcher,
)
from tests.server.helpers import websocket_scope as _websocket_scope

pytestmark = pytest.mark.asyncio

_HOST_ID = "3f866cafac81246fb60ae6ceb1a738da"


def _make_hello(
    name: str = "test-laptop",
    runners: list[str] | None = None,
) -> str:
    """Encode a HostHelloFrame for tests.

    :param name: Host name.
    :param runners: Live runner IDs.
    :returns: JSON-encoded hello frame.
    """
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
            runners=runners or [],
        )
    )


@pytest.fixture()
def binding_app(
    db_uri: str,
    request: pytest.FixtureRequest,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """App with host tunnel + REST routes for binding tests.

    :param db_uri: SQLite URI.
    :returns: Tuple of (app, registry, host_store, conv_store).
    """
    registry = HostRegistry(
        mcp_disabled_servers=bool(getattr(request, "param", None)),
        feature_flags=FeatureFlags(
            frozenset({Feature.MCP_DISABLED_SERVERS_CLAUDE, Feature.MCP_DISABLED_SERVERS_CODEX})
        ),
    )
    host_store = HostStore(db_uri)
    registry.launch_authorizer = host_store.admit_launch
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(registry, host_store),
        prefix="/v1",
    )
    app.include_router(
        create_hosts_router(registry, host_store, conv_store),
        prefix="/v1",
    )
    return app, registry, host_store, conv_store


async def _connect_host(
    app: FastAPI,
    registry: HostRegistry,
    host_id: str = _HOST_ID,
    name: str = "test-laptop",
    runners: list[str] | None = None,
) -> ApplicationCommunicator:
    """Connect a mock host and wait for registration.

    :param app: FastAPI app.
    :param registry: Host registry.
    :param host_id: Host identifier.
    :param name: Host name.
    :param runners: Live runner IDs for hello frame.
    :returns: Connected ASGI communicator.
    """
    path = f"/v1/hosts/{host_id}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"

    await comm.send_input(
        {"type": "websocket.receive", "text": _make_hello(name, runners)},
    )
    while registry.get(host_id) is None:
        await asyncio.sleep(0.01)
    return comm


@pytest.mark.parametrize("binding_app", [None, ["claude", "codex"]], indirect=True)
async def test_launch_runner_writes_host_id_and_runner_id(
    binding_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    request: pytest.FixtureRequest,
) -> None:
    """
    Verify that POST /hosts/{id}/runners writes both runner_id and
    host_id to the session row.

    If either is missing after the call, the binding flow in the
    launch endpoint is incomplete and reconnect reconciliation
    won't be able to find the session on host reconnect.
    """
    app, registry, _hs, conv_store = binding_app
    activation = request.node.callspec.params["binding_app"]
    comm = await _connect_host(app, registry)
    conv = conv_store.create_conversation(agent_id=None)

    async def _respond_launched() -> None:
        """Respond to the launch frame from the host."""
        for _ in range(20):
            # Binding writes finish before the launch frame; allow for CI database latency.
            output = await comm.receive_output(timeout=10.0)
            if output["type"] != "websocket.send":
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostLaunchRunnerFrame):
                assert frame.mcp_disabled_servers_active_harnesses == activation
                assert ("mcp_disabled_servers_active_harnesses" in json.loads(output["text"])) is (
                    activation is not None
                )
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostLaunchRunnerResultFrame(
                                request_id=frame.request_id,
                                status="launched",
                                runner_id="runner_token_test",
                            )
                        ),
                    }
                )
                return

    responder = asyncio.create_task(_respond_launched())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": "/tmp"},
        )

    await responder
    assert resp.status_code == 200

    updated = conv_store.get_conversation(conv.id)
    assert updated is not None
    # Both runner_id and host_id should be set.
    assert updated.runner_id is not None, "runner_id should be written to session row"
    assert updated.runner_id.startswith("runner_token_"), "runner_id should be a token-bound id"
    assert updated.host_id == _HOST_ID, "host_id should be written to session row"


async def _launch_and_capture_launch_args(
    app: FastAPI,
    comm: ApplicationCommunicator,
    conv_store: SqlAlchemyConversationStore,
    session_id: str,
    host_path_id: str = _HOST_ID,
) -> list[str] | None:
    """Launch a runner for ``session_id`` and return its stored args as the launch arrives.

    The runner reads the args as it starts, so that is the moment they must be right.
    """
    at_launch: list[list[str] | None] = []

    async def _respond_launched() -> None:
        for _ in range(20):
            output = await comm.receive_output(timeout=10.0)
            if output["type"] != "websocket.send":
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostLaunchRunnerFrame):
                row = conv_store.get_conversation(session_id)
                assert row is not None
                at_launch.append(row.terminal_launch_args)
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostLaunchRunnerResultFrame(
                                request_id=frame.request_id,
                                status="launched",
                                runner_id="runner_token_test",
                            )
                        ),
                    }
                )
                return

    responder = asyncio.create_task(_respond_launched())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{host_path_id}/runners",
            json={"session_id": session_id, "workspace": "/tmp"},
        )
    assert resp.status_code == 200, resp.text
    await responder
    assert len(at_launch) == 1
    return at_launch[0]


def _claude_source(conv_store: SqlAlchemyConversationStore, host_id: str) -> Conversation:
    """A Claude Code session on ``host_id`` started with openui's laptop-local args."""
    return conv_store.create_conversation(
        agent_id=None,
        host_id=host_id,
        workspace="/Users/someone/repo",
        terminal_launch_args=[
            "--mcp-config",
            "/Users/someone/.openui/mcp-inject.json",
            "--permission-mode",
            "plan",
        ],
        labels={
            "omnigent.wrapper": "claude-code-native-ui",
            "omnigent.claude_native.permission_mode": "plan",
        },
    )


async def test_launch_runner_drops_launch_args_a_fork_copied_from_another_host(
    binding_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A first cross-host launch drops copied extras; later args are the fork's own and stay."""
    app, registry, _hs, conv_store = binding_app
    comm = await _connect_host(app, registry)
    source = _claude_source(conv_store, "1a2b3c4d5e6f40718293a4b5c6d7e8f9")
    fork = conv_store.fork_conversation(source_conversation_id=source.id)

    first = await _launch_and_capture_launch_args(app, comm, conv_store, fork.id)
    assert first == ["--permission-mode", "plan"]
    kept = conv_store.get_conversation(source.id)
    assert kept is not None
    assert kept.terminal_launch_args == source.terminal_launch_args

    # A resume saves this host's own args; its launch fails and the rollback
    # clears the binding, then the user retries on the same host.
    conv_store.clear_runner_id(fork.id)
    own_args = ["--mcp-config", "/home/me/servers.json", "--permission-mode", "acceptEdits"]
    conv_store.update_conversation(fork.id, terminal_launch_args=own_args)
    conv_store.clear_host_binding(fork.id)
    retry = await _launch_and_capture_launch_args(app, comm, conv_store, fork.id)
    assert retry == own_args


async def test_launch_runner_keeps_launch_args_of_a_fork_on_its_source_host(
    binding_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A fork launched on its source's host keeps every arg, whatever host-id spelling is used."""
    app, registry, _hs, conv_store = binding_app
    comm = await _connect_host(app, registry)
    source = _claude_source(conv_store, _HOST_ID)
    fork = conv_store.fork_conversation(source_conversation_id=source.id)
    dashed_host_id = str(uuid.UUID(_HOST_ID))

    launched = await _launch_and_capture_launch_args(
        app, comm, conv_store, fork.id, host_path_id=dashed_host_id
    )

    assert launched == source.terminal_launch_args


async def test_sandbox_fork_cannot_launch_on_ordinary_host(
    binding_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, registry, _hs, conv_store = binding_app
    comm = await _connect_host(app, registry)
    source = conv_store.create_conversation(
        inference_snapshot={"runtime_config": {"providers": {}}}
    )
    fork = conv_store.fork_conversation(source.id)
    assert fork.inference_snapshot == source.inference_snapshot
    send = Mock(wraps=registry.send_text)
    monkeypatch.setattr(registry, "send_text", send)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            with pytest.raises(OmnigentError, match="saved sandbox inference profile") as error:
                await client.post(
                    f"/v1/hosts/{_HOST_ID}/runners",
                    json={"session_id": fork.id, "workspace": "/tmp"},
                )
        assert error.value.code == ErrorCode.INVALID_INPUT
        send.assert_not_called()
        saved = conv_store.get_conversation(fork.id)
        assert saved is not None
        assert saved.host_id is None
        assert saved.runner_id is None
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait(timeout=1.0)


async def test_host_id_in_session_response(
    binding_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify that host_id appears in session responses after being set.

    If host_id is missing from the response, the Web UI can't show
    which host the session is running on.
    """
    app, registry, _hs, conv_store = binding_app
    # Connecting upserts the host row so the conversation's host_id FK
    # has a target; the communicator itself isn't needed afterwards.
    await _connect_host(app, registry)
    # workspace is required when host_id is set (DB constraint
    # ck_conversations_workspace_required_for_host); the route layer
    # derives it from the user pick + agent.cwd boundary at session
    # create — this lower-level test passes a ready-made path.
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id=_HOST_ID,
        workspace="/Users/corey/projects/myapp",
    )

    # host_id should be persisted and returned via get_conversation.
    fetched = conv_store.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.host_id == _HOST_ID


# ── Managed (server-launched sandbox) host sessions ─────────


@pytest_asyncio.fixture()
async def managed_session_env(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> AsyncIterator[ManagedSessionEnv]:
    """Full app wired for managed-host sessions (no real sandbox).

    Builds the production ``create_app`` with host + managed-host
    stores and a modal ``sandbox:`` config, so a
    ``host_type="managed"`` create exercises the real route, tunnel,
    and store paths end to end. Only the sandbox itself is fake.

    :param runtime_init: Ensures runtime singletons are initialized.
    :param db_uri: SQLite URI shared by every store in the app.
    :param tmp_path: Per-test scratch dir for artifact/cache stores.
    :returns: The assembled environment.
    """
    app, host_store, conv_store = _managed_app(db_uri, tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield ManagedSessionEnv(
            app=app,
            client=client,
            host_store=host_store,
            conv_store=conv_store,
        )


def _managed_app(
    db_uri: str, root: Path
) -> tuple[FastAPI, HostStore, SqlAlchemyConversationStore]:
    """
    Build one server replica wired for managed-host sessions.

    Replicas built on the same *db_uri* share storage but keep their own
    in-memory host tunnels and launch tracker, like separate server pods.

    :param db_uri: SQLite URI shared by every store in the app.
    :param root: Scratch dir for this replica's artifact and cache stores.
    :returns: The app and its host and conversation stores.
    """
    artifact_store = LocalArtifactStore(str(root / "artifacts"))
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conv_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=root / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=host_store,
        # The production YAML parse path: its modal factory resolves
        # ModalSandboxLauncher at call time, which the test substitutes
        # via install_fake_modal_launcher.
        sandbox_config=parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://managed-test.example.com",
                "modal": {"image": "docker.io/test/omnigent-host:latest"},
            }
        ),
    )
    return app, host_store, conv_store


@dataclass
class ManagedSessionEnv:
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


async def _fake_sandbox_host(
    app: FastAPI, host_id: str, host_name: str, token: str
) -> ApplicationCommunicator:
    """Act as the host process inside the (fake) sandbox.

    Connects to the app's real host tunnel authenticating ONLY with
    the launch token (exactly what a sandbox has), sends hello with
    the server-injected host name (the real host reads it from
    ``OMNIGENT_HOST_NAME``; the name must match the pre-registered
    row's ``(owner, name)`` key), then answers the launch frame with a
    launched result — the full protocol a real managed host performs.

    :param app: The app whose tunnel to dial.
    :param host_id: Server-chosen host identity from the launch env.
    :param host_name: Server-chosen host name from the launch env.
    :param token: Raw launch token from the launch env.
    :returns: The live tunnel communicator. The CALLER must keep it
        referenced for as long as the host should stay online —
        dropping it garbage-collects the ASGI task, which tears the
        tunnel down and flips the host offline.
    """
    from omnigent.runner.identity import token_bound_runner_id

    scope = _websocket_scope(f"/v1/hosts/{host_id}/tunnel")
    scope["headers"] = [(b"x-omnigent-host-token", token.encode("ascii"))]
    comm = ApplicationCommunicator(app, scope)
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=5.0)
    assert accepted["type"] == "websocket.accept", f"tunnel refused: {accepted!r}"
    await comm.send_input(
        {"type": "websocket.receive", "text": _make_hello(name=host_name)},
    )
    # Serve frames until the launch request arrives, then confirm it.
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
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostLaunchRunnerResultFrame(
                            request_id=frame.request_id,
                            status="launched",
                            runner_id=token_bound_runner_id(frame.binding_token),
                        )
                    ),
                }
            )
            return comm
    raise AssertionError("fake sandbox host never received a launch frame")


async def _wait_for_managed_binding(
    env: ManagedSessionEnv,
    session_id: str,
    *,
    timeout_s: float = 15.0,
) -> Conversation:
    """
    Poll the session row until the background managed launch binds it.

    The managed create returns before the sandbox exists; the
    background task writes ``host_id`` / ``workspace`` / ``runner_id``
    once the (fake) sandbox host registers. Polling the store is the
    same observation a client makes via ``GET /v1/sessions/{id}``.

    :param env: The managed-session test environment.
    :param session_id: The created session's id.
    :param timeout_s: Poll budget; a binding regression fails here.
    :returns: The bound conversation row.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        conv = env.conv_store.get_conversation(session_id)
        assert conv is not None, "session row vanished while awaiting managed binding"
        # A move holds a claim on runner_id while it provisions; the host
        # binding is what marks the launch done.
        if conv.host_id is not None and conv.runner_id is not None:
            return conv
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"background managed launch never bound session {session_id} within {timeout_s}s"
    )


@pytest.mark.parametrize("requested_host_id", [None, "0123456789abcdef0123456789abcdef"])
async def test_managed_session_create_end_to_end(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    requested_host_id: str | None,
) -> None:
    """
    ``POST /v1/sessions`` with ``host_type="managed"`` returns
    immediately and provisions in the background: a sandbox is
    provisioned, the sandbox host registers over the REAL tunnel with
    the minted token, and the session ends up bound to that host with
    a runner launched on it — everything but the sandbox is production
    code. A create that names the host's id gets a host registered under
    it, recorded in the ``omnigent.sandbox.requested_host_id`` label until
    the host binds.
    """
    env = managed_session_env
    # A healthy fake host registers in well under a second; shrink the
    # online-poll budget so a registration regression fails the test in
    # seconds instead of hanging into the pytest timeout.
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    # No real runner ever connects in this harness; shrink the
    # background task's runner-tunnel wait so it settles (and the
    # task finishes) within the test instead of lingering 30s.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it.

        Runs on a worker thread (the launcher executes under
        ``asyncio.to_thread``), so the coroutine is handed back to the
        app's event loop. The resolved communicators are awaited (and
        thereby kept referenced — see ``_fake_sandbox_host``) by the
        test body.
        """
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    # A tab that lists the row on the create's ``session_added`` push routes
    # the session by the requested-host label until the host binds, so the
    # label must already be on the row when the push goes out: a row read
    # without it would clear that routing in the tab that created it.
    from omnigent.server.routes.sessions import routes_core

    labels_when_announced: dict[str, list[dict[str, str]]] = {}
    real_announce = routes_core._announce_session_added

    def _announce_recording_labels(user_id: str | None, session_id: str) -> None:
        row = env.conv_store.get_conversation(session_id)
        labels_when_announced.setdefault(session_id, []).append(
            dict(row.labels) if row is not None else {}
        )
        real_announce(user_id, session_id)

    monkeypatch.setattr(routes_core, "_announce_session_added", _announce_recording_labels)
    # Stronger still, the label is born with the row: a sidebar list read that is
    # already in flight when the create runs can't return the row without it.
    real_create = env.conv_store.create_conversation
    labels_at_insert: dict[str, dict[str, str]] = {}

    def _create_recording_labels(*args: object, **kwargs: object) -> Conversation:
        created = real_create(*args, **kwargs)  # type: ignore[arg-type]
        labels = kwargs.get("labels")
        labels_at_insert[created.id] = dict(labels) if isinstance(labels, dict) else {}
        return created

    monkeypatch.setattr(env.conv_store, "create_conversation", _create_recording_labels)

    agent = await create_test_agent(env.client, name="managed-host-agent")
    body: dict[str, object] = {"agent_id": agent["id"], "host_type": "managed"}
    if requested_host_id is not None:
        body["host_id"] = requested_host_id
    resp = await env.client.post("/v1/sessions", json=body)
    assert resp.status_code == 201, resp.text
    # The non-blocking contract: the create response carries no host
    # binding — provisioning is still running in the background.
    assert resp.json()["host_id"] is None
    assert resp.json()["labels"].get("omnigent.sandbox.requested_host_id") == requested_host_id
    session_id = resp.json()["id"]
    assert labels_at_insert[session_id].get("omnigent.sandbox.requested_host_id") == (
        requested_host_id
    )
    assert [
        labels.get("omnigent.sandbox.requested_host_id")
        for labels in labels_when_announced[session_id]
    ] == [requested_host_id]

    # The background launch binds the session row: sandbox host,
    # sandbox workspace, and a token-bound runner.
    conv = await _wait_for_managed_binding(env, session_id)
    # Keep the fake hosts' tunnels alive (referenced) for the rest of
    # the test — the host store flips them offline if they drop.
    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1
    assert conv.host_id is not None
    if requested_host_id is not None:
        assert conv.host_id == requested_host_id
    assert conv.labels.get("omnigent.sandbox.requested_host_id") == requested_host_id
    assert conv.workspace == "/root/workspace"
    assert conv.runner_id is not None
    assert conv.runner_id.startswith("runner_token_")

    # The hosts row carries the sandbox backing and is owned by the
    # caller — no auth provider on this app → the reserved local user,
    # same as a directly-connected host would be.
    host = env.host_store.get_host(conv.host_id)
    assert host is not None
    assert host.user_id == RESERVED_USER_LOCAL
    assert host.status == "online"
    assert host.sandbox_provider == "modal"
    assert host.sandbox_id == "sb-fake-1"
    assert host.name == f"managed-{conv.host_id[:8]}"

    # Deleting the managed session tears the sandbox down: the
    # provider terminate fires and the host row is deleted — which
    # both revokes the launch token and removes the dead host from
    # the picker (no offline ghost lingering after the session).
    delete_resp = await env.client.delete(f"/v1/sessions/{session_id}")
    assert delete_resp.status_code == 200, delete_resp.text
    # Provider terminate is best-effort and idempotent: with no runner ever
    # connecting here, the background launch's failure teardown can race the
    # delete's teardown and both fire it. Assert the sandbox (and only it) was
    # torn down, not the number of best-effort attempts.
    assert set(fake.terminated) == {"sb-fake-1"}
    assert env.host_store.get_host(conv.host_id) is None
    assert env.host_store.list_hosts(RESERVED_USER_LOCAL) == []
    # The tunnels list holds the fake hosts open through the delete;
    # release them only now.
    del tunnels


async def test_deleting_one_session_keeps_a_shared_sandbox_host(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Several sessions can be bound to one sandbox host (a runner launch on
    the host binds its session there). Deleting one keeps the sandbox for
    the others; deleting the last tears it down.
    """
    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="shared-host-agent")
    resp = await env.client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "host_type": "managed"}
    )
    assert resp.status_code == 201, resp.text
    first = await _wait_for_managed_binding(env, resp.json()["id"])
    tunnels = [await future for future in host_futures]
    assert first.host_id is not None

    resp = await env.client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    second_id = resp.json()["id"]
    env.conv_store.set_host_id(second_id, first.host_id, "/root/workspace")

    delete_resp = await env.client.delete(f"/v1/sessions/{first.id}")
    assert delete_resp.status_code == 200, delete_resp.text
    assert fake.terminated == []
    assert env.host_store.get_host(first.host_id) is not None

    delete_resp = await env.client.delete(f"/v1/sessions/{second_id}")
    assert delete_resp.status_code == 200, delete_resp.text
    assert set(fake.terminated) == {"sb-fake-1"}
    assert env.host_store.get_host(first.host_id) is None
    del tunnels


async def test_managed_session_create_with_repo_workspace_binds_cloned_dir(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /v1/sessions`` with ``host_type="managed"`` and a
    ``<repo>#<branch>`` workspace clones the repository inside the
    sandbox and binds the session to the CLONED directory — the full
    route threading (schema → parse → launch → conversation row), not
    just the launch helper.
    """
    env = managed_session_env
    # Same shrunken online-poll budget as the e2e golden path: a
    # registration regression should fail in seconds.
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    # No real runner connects; settle the background task quickly.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it."""
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-repo-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_type": "managed",
            "workspace": "https://github.com/org/myrepo.git#main",
        },
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    conv = await _wait_for_managed_binding(env, session_id)
    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1

    # The clone ran in the sandbox with the requested branch pinned…
    assert (
        "git clone --branch main --single-branch "
        "-- https://github.com/org/myrepo.git /root/workspace/myrepo"
    ) in fake.commands
    # …and the session is bound to the cloned directory, which is also
    # what a client sees on the snapshot once the launch settles.
    assert conv.workspace == "/root/workspace/myrepo"
    assert conv.runner_id is not None
    snapshot = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["workspace"] == "/root/workspace/myrepo"
    del tunnels


@pytest.mark.parametrize("pending_cleanup", [False, True])
@pytest.mark.parametrize(
    "host_id",
    [
        "0123456789abcdef0123456789abcdef",
        # A different id whose derived name (``managed-01234567``) is taken.
        "01234567ffffffffffffffffffffffff",
    ],
)
async def test_managed_session_create_rejects_a_taken_host_id(
    managed_session_env: ManagedSessionEnv, host_id: str, pending_cleanup: bool
) -> None:
    """
    A managed create the launch couldn't register is a 409 that creates no
    session, including against a deleted host whose sandbox awaits cleanup.
    """
    env = managed_session_env
    env.host_store.register_managed_host(
        host_id="0123456789abcdef0123456789abcdef",
        name="managed-01234567",
        user_id=RESERVED_USER_LOCAL,
        token="tok-taken",
        provider="modal",
        sandbox_id="sb-taken",
        token_expires_at=9_999_999_999,
    )
    if pending_cleanup:
        env.host_store.delete_host("0123456789abcdef0123456789abcdef")
        assert env.host_store.get_host("0123456789abcdef0123456789abcdef") is None
    agent = await create_test_agent(env.client, name="managed-taken-host-agent")
    sessions_before = env.conv_store.list_conversations(limit=100)

    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed", "host_id": host_id},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "already_exists"
    assert env.conv_store.list_conversations(limit=100) == sessions_before


async def test_managed_multipart_bundle_create_end_to_end(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Multipart ``POST /v1/sessions`` with ``host_type="managed"`` uploads
    a bundle AND provisions a sandbox in one call — the gap this feature
    closes. The freshly-created session-scoped agent is what the managed
    runner runs on the sandbox, so the same background launch as the JSON
    path binds the session to a sandbox host with a runner. Also confirms
    the caller owns the session (grant happens BEFORE the managed launch
    is scheduled, so a launch failure can't orphan an unowned row).
    """
    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it."""
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    bundle = build_agent_bundle(name="managed-multipart-e2e-agent")
    resp = await env.client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"host_type": "managed"})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session_id"]

    # The uploaded bundle's session-scoped agent is what runs on the
    # provisioned sandbox — the background launch binds the session the
    # same way the JSON path does.
    conv = await _wait_for_managed_binding(env, session_id)
    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1
    assert conv.host_id is not None
    assert conv.workspace == "/root/workspace"
    assert conv.runner_id is not None
    assert conv.runner_id.startswith("runner_token_")

    host = env.host_store.get_host(conv.host_id)
    assert host is not None
    assert host.user_id == RESERVED_USER_LOCAL
    assert host.status == "online"
    assert host.sandbox_provider == "modal"
    assert host.sandbox_id == "sb-fake-1"
    del tunnels


async def test_managed_multipart_bundle_create_rejects_path_workspace(
    managed_session_env: ManagedSessionEnv,
) -> None:
    """
    A multipart managed create with a filesystem-path workspace is
    rejected at metadata validation — a managed workspace is a git
    repository URL, mirroring the JSON path's contract — before any
    sandbox provisioning is scheduled.
    """
    bundle = build_agent_bundle(name="managed-multipart-badws-agent")
    resp = await managed_session_env.client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"host_type": "managed", "workspace": "/tmp/w"})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code >= 400, resp.text
    assert "git repository URL" in resp.text


async def test_managed_session_create_validator_errors_serialize_as_422(
    managed_session_env: ManagedSessionEnv,
) -> None:
    """
    A model_validator rejection (here: a path workspace on a managed
    create) reaches the client as a REAL 422 whose ``detail[0].msg``
    carries the message — the shape the web UI's describeCreateError
    renders. Regression guard: ``exc.errors()`` used to embed the raw
    ``ValueError`` in ``ctx``, which JSONResponse cannot serialize, so
    every validator 422 on this route 500'd as ``internal_error``.
    """
    resp = await managed_session_env.client.post(
        "/v1/sessions",
        json={
            "agent_id": "d7a89f58205a70539a16fa4b7bd06270",
            "host_type": "managed",
            "workspace": "/tmp/w",
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    # The list-of-errors shape with a human-readable msg is what
    # describeCreateError picks the message from.
    assert isinstance(detail, list) and len(detail) == 1
    assert "git repository URL" in detail[0]["msg"]


async def test_managed_session_create_rejects_unconfigured_provider(
    managed_session_env: ManagedSessionEnv,
) -> None:
    """
    A create naming a provider the server doesn't offer is a synchronous
    400 (validated at POST, before the background launch), and the error
    names what IS available so the user can correct it. This env offers
    only modal, so 'nope' is rejected with modal listed.
    """
    agent = await create_test_agent(managed_session_env.client, name="managed-bad-provider-agent")
    resp = await managed_session_env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed", "sandbox_provider": "nope"},
    )
    assert resp.status_code == 400, resp.text
    assert "nope" in resp.text
    assert "modal" in resp.text


async def test_managed_session_create_without_config_fails_clearly(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    ``host_type="managed"`` on a server with no ``sandbox:`` config
    must fail with a clear actionable error — not a 500 from a missing
    attribute deep in the launch path.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        agent = await create_test_agent(client, name="managed-unconfigured-agent")
        resp = await client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "host_type": "managed"},
        )
    assert resp.status_code == 400, resp.text
    assert "managed hosts are not configured" in resp.text


async def test_managed_create_returns_during_provision_and_message_rendezvouses(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The managed create is non-blocking, and a message racing the
    provision is saved for the launch to deliver instead of failing fast.

    The fake launcher holds ``provision`` on a gate, pinning the
    background launch mid-provision deterministically. While held:

    - The create POST has ALREADY returned (with the old synchronous
      create, the POST itself would block on the gate and this test
      would hang at the first await).
    - A message POST is accepted and saved — pre-fix it returned 503
      "No runner bound for session" immediately, desyncing the Web
      UI's auto-sent first prompt from the provisioning sandbox.

    Releasing the gate lets the launch proceed into a host-start
    failure, which must surface the recorded reason on later message
    POSTs, report the saved message as undelivered, and tear the sandbox down.
    """
    import threading

    env = managed_session_env
    gate = threading.Event()
    fake = FakeSandboxLauncher(provision_gate=gate, fail_on_host_start=True)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-rendezvous-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    # The create returned while provision is still gate-held — the
    # non-blocking contract. (The pre-fix synchronous create could not
    # produce this response until the gate was released.)
    assert resp.status_code == 201, resp.text
    assert resp.json()["host_id"] is None
    session_id = resp.json()["id"]

    # The seeded progress stage is on the snapshot from the moment the
    # create returns — what the navigating Web UI cold-loads.
    snapshot = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["sandbox_status"] == {
        "stage": "provisioning",
        "error": None,
        "provider": "modal",
        "details": None,
    }

    message_resp = await env.client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello sandbox"}],
            },
        },
    )
    # Saved while provisioning is held, for delivery after launch.
    assert message_resp.status_code == 202, message_resp.text
    assert message_resp.json()["queued"] is True

    gate.set()
    tracker = env.app.state.managed_launches
    for _ in range(150):
        launch = tracker.get(session_id)
        if launch is not None and launch.settled.is_set():
            break
        await asyncio.sleep(0.1)
    assert launch is not None and launch.error is not None
    # Replicas without this tracker entry see the failure on the session.
    for _ in range(50):
        conv = env.conv_store.get_conversation(session_id)
        if conv is not None and conv.labels.get("omnigent.startup") == "failed":
            break
        await asyncio.sleep(0.1)
    assert conv is not None and conv.labels.get("omnigent.startup") == "failed"

    # A later message hits the retained failure record immediately; the
    # launch failed at host start, so it reports that reason rather than
    # a generic "no runner bound".
    late_resp = await env.client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "retry"}],
            },
        },
    )
    assert late_resp.status_code == 503, late_resp.text
    assert "managed sandbox failed to launch" in late_resp.text

    # The failed launch tore the sandbox down (launch_managed_host's
    # cleanup path), so nothing leaks until the provider lifetime cap.
    assert fake.terminated == ["sb-fake-1"]

    # The failure is retained on the snapshot (mirroring the tracker's
    # failed-entry retention) so a client reloading the dead session
    # still sees why the sandbox never came up, not a blank chat.
    failed_snapshot = await env.client.get(f"/v1/sessions/{session_id}")
    assert failed_snapshot.status_code == 200, failed_snapshot.text
    sandbox_status = failed_snapshot.json()["sandbox_status"]
    assert sandbox_status["stage"] == "failed"
    assert "host startup failed" in sandbox_status["error"]
    # No sandbox is left to deliver the saved message; the transcript says so.
    assert failed_snapshot.json()["pending_inputs"] == []
    errors = [item for item in failed_snapshot.json()["items"] if item["type"] == "error"]
    assert any("host startup failed" in item["data"]["message"] for item in errors), errors


# ── Moving an existing session onto a new managed sandbox ───

_LAPTOP_HOST_ID = "4b1f2e3d4c5b6a798877665544332211"
_MOVED_SANDBOX_HOST_ID = "0123456789abcdef0123456789abcdef"
# Left by a sandbox the session ran in before it was resumed on the laptop.
_STALE_SANDBOX_LABELS = {
    "omnigent.sandbox.repo.0": "https://github.com/org/old-repo",
    "omnigent.sandbox.requested_host_id": "feedfacefeedfacefeedfacefeedface",
}


async def _session_released_from_laptop(env: ManagedSessionEnv, *, agent_name: str) -> str:
    """
    Create a session that ran on a user-connected host, with its runner released.

    The state Switch host leaves a session in before it asks for a sandbox: the
    ``PATCH runner_id=""`` release keeps the old host and directory bound.

    :param env: The managed-session test environment.
    :param agent_name: Name for the session's freshly created agent.
    :returns: The session id.
    """
    agent = await create_test_agent(env.client, name=agent_name)
    resp = await env.client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    env.host_store.upsert_on_connect(_LAPTOP_HOST_ID, "laptop", RESERVED_USER_LOCAL)
    env.conv_store.set_host_id(session_id, _LAPTOP_HOST_ID, "/Users/alice/repo", "feature/x")
    assert env.conv_store.set_runner_id(session_id, "runner_laptop")
    release = await env.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": ""})
    assert release.status_code == 200, release.text
    return session_id


async def test_session_moves_from_its_host_onto_a_new_managed_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /v1/sessions/{id}/managed-host`` moves a session off a connected host
    onto a fresh sandbox through the same background launch a managed create
    uses: it returns hostless and provisioning, then the sandbox host registers
    over the real tunnel and the session binds to it with a runner. The machine
    it left stays registered, and an earlier sandbox's labels don't follow.
    """
    import threading

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    _start_fake_sandbox_host, host_futures = _fake_host_connector(env, asyncio.get_running_loop())

    # Held at provision so the response is read before the launch moves on.
    provisioned = threading.Event()
    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host, provision_gate=provisioned)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-to-sandbox-agent")
    env.conv_store.set_labels(session_id, _STALE_SANDBOX_LABELS)

    resp = await env.client.post(
        f"/v1/sessions/{session_id}/managed-host",
        json={"sandbox_provider": "modal", "host_id": _MOVED_SANDBOX_HOST_ID},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["host_id"] is None
    assert body["workspace"] is None
    assert body["sandbox_status"]["stage"] == "provisioning"
    assert body["labels"]["omnigent.sandbox.requested_host_id"] == _MOVED_SANDBOX_HOST_ID
    # No repository was asked for, so a relaunch must not clone the old one.
    assert "omnigent.sandbox.repo.0" not in body["labels"]
    moving = env.conv_store.get_conversation(session_id)
    assert moving is not None and moving.runner_id is not None
    provisioned.set()
    conv = await _wait_for_managed_binding(env, session_id)
    await poll_until_async(
        lambda: (
            (current := env.conv_store.get_conversation(session_id)) is not None
            and current.runner_id != moving.runner_id
        )
    )
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1
    assert conv.host_id == _MOVED_SANDBOX_HOST_ID
    assert conv.workspace == "/root/workspace"
    # The laptop's worktree branch doesn't exist in the sandbox's checkout.
    assert conv.git_branch is None
    assert conv.runner_id is not None and conv.runner_id.startswith("runner_token_")
    sandbox_host = env.host_store.get_host(_MOVED_SANDBOX_HOST_ID)
    assert sandbox_host is not None
    assert sandbox_host.sandbox_provider == "modal"
    assert sandbox_host.user_id == RESERVED_USER_LOCAL
    assert env.host_store.get_host(_LAPTOP_HOST_ID) is not None
    del tunnels


async def test_message_sent_while_a_moved_session_provisions_waits_for_the_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    While its new sandbox provisions, a moved session takes messages the way a
    managed create does: saved for the launch, not relaunched on the old host.
    A second move is refused meanwhile. When the launch fails, the session is
    left hostless with the reason on its snapshot, so the owner can resume it
    on a machine.
    """
    import threading

    env = managed_session_env
    gate = threading.Event()
    fake = FakeSandboxLauncher(provision_gate=gate, fail_on_host_start=True)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-rendezvous-agent")

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
    assert resp.status_code == 202, resp.text

    message = await env.client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
    )
    assert message.status_code == 202, message.text
    assert message.json()["queued"] is True
    again = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "conflict"

    gate.set()
    tracker = env.app.state.managed_launches
    for _ in range(150):
        launch = tracker.get(session_id)
        if launch is not None and launch.settled.is_set():
            break
        await asyncio.sleep(0.1)
    assert launch is not None and launch.error is not None
    snapshot = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["host_id"] is None
    assert snapshot.json()["runner_id"] is None
    assert snapshot.json()["sandbox_status"]["stage"] == "failed"
    assert fake.terminated == ["sb-fake-1"]
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == ["failed"]


async def test_concurrent_sandbox_moves_launch_one_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two moves racing for one session, such as from two tabs, launch a single
    sandbox; the later one is told the session's binding changed. The loser's
    rollback leaves the winner's launch alone: its startup and a message held
    for it stay, and when its sandbox then fails, it releases its claim.
    """
    import threading

    from omnigent.server.managed_hosts import is_move_claim

    env = managed_session_env
    gate = threading.Event()
    fake = FakeSandboxLauncher(provision_gate=gate, fail_on_host_start=True)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-race-agent")

    responses = await asyncio.gather(
        *(env.client.post(f"/v1/sessions/{session_id}/managed-host", json={}) for _ in range(2))
    )

    assert sorted(r.status_code for r in responses) == [202, 409], [r.text for r in responses]
    tracker = env.app.state.managed_launches
    assert tracker.get(session_id) is not None
    moving = env.conv_store.get_conversation(session_id)
    assert moving is not None and is_move_claim(moving.runner_id)
    assert moving.labels.get(STARTUP_LABEL) == "pending"
    held = await _post_user_message(env, session_id)
    assert held["queued"] is True
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == [
        "in_progress"
    ]
    gate.set()
    await poll_until_async(
        lambda: (launch := tracker.get(session_id)) is not None and launch.settled.is_set()
    )
    assert len(fake.provisioned_names) == 1
    await poll_until_async(
        lambda: (
            [item.status for item in env.conv_store.list_pending_inputs(session_id)] == ["failed"]
        )
    )
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.runner_id is None


async def test_runner_bound_during_a_sandbox_move_keeps_the_session(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A runner that binds after the move's checks but before it claims the
    session, such as another tab resuming it, wins: the move is refused and the
    runner keeps its binding.
    """
    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-toctou-agent")
    get_host = env.host_store.get_host

    def get_host_as_a_runner_binds(host_id: str) -> object:
        env.conv_store.set_runner_id(session_id, "runner_other_tab")
        return get_host(host_id)

    monkeypatch.setattr(env.host_store, "get_host", get_host_as_a_runner_binds)

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})

    assert resp.status_code == 409, resp.text
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.runner_id) == (_LAPTOP_HOST_ID, "runner_other_tab")
    assert env.app.state.managed_launches.get(session_id) is None


def _fake_host_connector(
    env: ManagedSessionEnv, loop: asyncio.AbstractEventLoop
) -> tuple[Callable[[HostStartInvocation], None], list[asyncio.Future[ApplicationCommunicator]]]:
    """
    Build an ``on_host_start`` hook that connects the fake sandbox host.

    :param env: The managed-session test environment.
    :param loop: The test's event loop, which the launcher's worker thread
        schedules the connection on.
    :returns: The hook and the futures of the hosts it connected.
    """
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def start(invocation: HostStartInvocation) -> None:
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    return start, host_futures


async def _post_user_message(env: ManagedSessionEnv, session_id: str) -> dict[str, Any]:
    """
    Send one user message to *session_id* and return the response body.

    :param env: The managed-session test environment.
    :param session_id: The session to message.
    :returns: The 202 response body.
    """
    resp = await env.client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def test_host_launch_while_the_sandbox_provisions_is_refused(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The move holds its claim on the session until the sandbox binds, so a
    connected host can't take the session meanwhile, on any replica. The sandbox
    then binds and replaces the claim with its own runner.
    """
    import threading

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    start, host_futures = _fake_host_connector(env, asyncio.get_running_loop())
    provisioned = threading.Event()
    fake = FakeSandboxLauncher(on_host_start=start, provision_gate=provisioned)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-held-claim-agent")

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
    assert resp.status_code == 202, resp.text
    moving = env.conv_store.get_conversation(session_id)
    assert moving is not None and moving.host_id is None and moving.runner_id is not None
    # The bind a connected-host launch makes first.
    assert not env.conv_store.set_runner_id(session_id, "runner_other_tab")
    provisioned.set()

    def claim_replaced() -> bool:
        current = env.conv_store.get_conversation(session_id)
        return (
            current is not None
            and current.host_id is not None
            and current.runner_id not in (None, moving.runner_id)
        )

    await poll_until_async(claim_replaced)
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.host_id != _LAPTOP_HOST_ID
    assert len([await future for future in host_futures]) == 1


async def test_released_claim_lets_another_host_keep_the_session(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    If the move's claim is released while the sandbox provisions and another
    host binds the session, that host keeps it: the sandbox doesn't overwrite
    the binding or supersede its runner, and is torn down. The launch settles
    without failing the session, and a message held for the sandbox is marked
    not sent instead of waiting for a sandbox that won't come.
    """
    import threading

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    start, host_futures = _fake_host_connector(env, asyncio.get_running_loop())
    provisioned = threading.Event()
    fake = FakeSandboxLauncher(on_host_start=start, provision_gate=provisioned)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-superseded-agent")

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
    assert resp.status_code == 202, resp.text
    held = await _post_user_message(env, session_id)
    assert held["queued"] is True
    env.conv_store.clear_runner_id(session_id)
    assert env.conv_store.set_runner_id(session_id, "runner_other_tab")
    env.conv_store.set_host_id(session_id, _LAPTOP_HOST_ID, "/Users/alice/repo")
    provisioned.set()

    def handed_off() -> bool:
        current = env.conv_store.get_conversation(session_id)
        return current is not None and current.labels.get(STARTUP_LABEL) == STARTUP_WITHDRAWN

    await poll_until_async(lambda: fake.terminated == ["sb-fake-1"])
    await poll_until_async(handed_off)
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.runner_id) == (
        _LAPTOP_HOST_ID,
        "/Users/alice/repo",
        "runner_other_tab",
    )
    assert env.app.state.managed_launches.get(session_id) is None
    snapshot = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["sandbox_status"] is None
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == ["failed"]
    assert len(host_futures) == 1


@pytest.mark.parametrize("rebind", ["before_cleanup", "after_claim_release"])
async def test_released_claim_keeps_the_winner_when_the_old_sandbox_fails_to_start(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    rebind: str,
) -> None:
    """
    A move whose sandbox fails to start settles while it still holds the
    session, and only then releases its claim. A host that took the session
    before (here, as a lapsed claim would allow) keeps it: its startup isn't
    marked failed, and a message held for the sandbox is marked not sent. One
    that takes it once the claim is released gets no write from the failed
    launch: the failure was already recorded, while the session was the move's.
    """
    import threading

    from omnigent.server.managed_hosts import is_move_claim
    from omnigent.server.routes._sessions.common import _managed_launch_tasks

    env = managed_session_env
    provisioned = threading.Event()
    fake = FakeSandboxLauncher(provision_gate=provisioned, fail_on_host_start=True)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-obsolete-agent")

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
    assert resp.status_code == 202, resp.text
    held = await _post_user_message(env, session_id)
    assert held["queued"] is True

    def take_over() -> None:
        env.conv_store.clear_runner_id(session_id)
        assert env.conv_store.set_runner_id(session_id, "runner_other_tab")
        env.conv_store.set_host_id(session_id, _LAPTOP_HOST_ID, "/Users/alice/repo")
        # The winner's own startup state, which the failed launch must not touch.
        env.conv_store.set_labels(session_id, {STARTUP_LABEL: "ready"})

    took_over: list[bool] = []
    if rebind == "before_cleanup":
        take_over()
    else:
        clear_runner_id = env.conv_store.clear_runner_id

        def release_then_take_over(conversation_id: str, **kwargs: Any) -> Conversation:
            cleared = clear_runner_id(conversation_id, **kwargs)
            expected = kwargs.get("expected_binding")
            if expected is not None and is_move_claim(expected.runner_id) and not took_over:
                took_over.append(True)
                take_over()
            return cleared

        monkeypatch.setattr(env.conv_store, "clear_runner_id", release_then_take_over)
    provisioned.set()

    def settled() -> bool:
        current = env.conv_store.get_conversation(session_id)
        launch = env.app.state.managed_launches.get(session_id)
        return (
            current is not None
            and current.runner_id == "runner_other_tab"
            and (launch is None or launch.settled.is_set())
            and [item.status for item in env.conv_store.list_pending_inputs(session_id)]
            == ["failed"]
        )

    await poll_until_async(settled)
    # Let anything the failed launch still had queued land.
    await asyncio.gather(*list(_managed_launch_tasks), return_exceptions=True)
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.runner_id) == (
        _LAPTOP_HOST_ID,
        "/Users/alice/repo",
        "runner_other_tab",
    )
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == ["failed"]
    if rebind == "before_cleanup":
        assert conv.labels.get(STARTUP_LABEL) == STARTUP_WITHDRAWN
        assert env.app.state.managed_launches.get(session_id) is None
        snapshot = await env.client.get(f"/v1/sessions/{session_id}")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["sandbox_status"] is None
        assert snapshot.json()["status"] != "failed"
    else:
        assert took_over
        assert conv.labels.get(STARTUP_LABEL) == "ready"


async def test_relaunch_that_reads_a_move_claim_is_refused(
    managed_session_env: ManagedSessionEnv,
) -> None:
    """
    A relaunch that reads the session while a move holds it, from any caller,
    refuses to replace the move's claim: only the move's own sandbox launch may.
    """
    from typing import cast

    from omnigent.server.managed_hosts import new_move_claim
    from omnigent.server.routes._sessions.helpers import (
        _launch_runner_on_host_impl,
        _launch_runner_on_host_locked,
    )

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-claim-relaunch-agent")
    claim = new_move_claim()
    assert env.conv_store.set_runner_id(session_id, claim)
    seen = env.conv_store.get_conversation(session_id)
    assert seen is not None

    class _NoAdmission:
        async def admit_launch(self, conn: object, conversation_id: str, **_: object) -> None:
            raise AssertionError("a refused relaunch must not reach admission")

    attempt = await _launch_runner_on_host_locked(
        seen,
        env.conv_store,
        cast(Any, _NoAdmission()),
        cast(Any, SimpleNamespace(host_id=_LAPTOP_HOST_ID)),
    )

    assert attempt.error_code == "session_binding_changed"
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.runner_id == claim

    # A caller that read the session before the move claimed it finds the claim
    # under the relaunch lock, and gets the same refusal rather than the claim
    # back as a runner to wait for.
    unclaimed = replace(seen, runner_id=None)
    late = await _launch_runner_on_host_impl(
        unclaimed,
        env.conv_store,
        cast(Any, _NoAdmission()),
        cast(Any, SimpleNamespace(host_id=_LAPTOP_HOST_ID)),
    )

    assert (late.runner_id, late.error_code) == (claim, "session_binding_changed")


async def test_message_relaunching_as_a_move_claims_waits_for_the_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A message that found the session unbound, and relaunches on its host just
    as a move claims it, is held for the sandbox like one sent after the claim,
    rather than waiting on the claim as if it were a runner and failing.
    """
    from omnigent.server.managed_hosts import is_move_claim, new_move_claim
    from omnigent.server.routes import sessions as sessions_facade

    env = managed_session_env
    session_id = await _session_released_from_laptop(
        env, agent_name="move-claim-relaunch-msg-agent"
    )
    claim = new_move_claim()
    admissions: list[str] = []

    # The user's laptop, connected (no tunnel is needed for the relaunch to start).
    laptop = SimpleNamespace(
        host_id=_LAPTOP_HOST_ID,
        owner=None,
        pending_launches={},
        registered_with_managed_token=False,
    )
    host_registry = env.app.state.host_registry
    get_connection = host_registry.get
    monkeypatch.setattr(
        host_registry,
        "get",
        lambda host_id: laptop if host_id == _LAPTOP_HOST_ID else get_connection(host_id),
    )

    async def admit_launch(conn: object, conversation_id: str, **_: object) -> None:
        admissions.append(conversation_id)

    monkeypatch.setattr(host_registry, "admit_launch", admit_launch)
    relaunch = sessions_facade._launch_runner_on_host

    async def claimed_as_it_relaunches(conv: Conversation, *args: Any, **kwargs: Any) -> Any:
        # The move claims the session between the message's read and its relaunch.
        env.conv_store.set_runner_id(conv.id, claim)
        return await relaunch(conv, *args, **kwargs)

    monkeypatch.setattr(sessions_facade, "_launch_runner_on_host", claimed_as_it_relaunches)

    body = await _post_user_message(env, session_id)

    assert body["queued"] is True and body.get("pending_id"), body
    assert admissions == []
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == [
        "in_progress"
    ]
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and is_move_claim(conv.runner_id)


async def test_move_launch_queued_behind_a_refused_relaunch_still_starts(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A relaunch refused for a move, such as a terminal recovery while the sandbox
    binds, isn't recorded as the session's launch: the move's own launch queued
    behind it still starts the sandbox's runner, rather than taking the refusal
    as its result and abandoning the sandbox.
    """
    import threading
    from typing import cast

    from omnigent.server.managed_hosts import is_move_claim, new_move_claim
    from omnigent.server.routes._sessions.helpers import _launch_runner_on_host_impl

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-relaunch-memo-agent")
    claim = new_move_claim()
    assert env.conv_store.set_runner_id(session_id, claim)
    # The sandbox host is bound; its runner isn't started yet.
    env.conv_store.set_host_id(session_id, _MOVED_SANDBOX_HOST_ID, "/workspace")
    seen = env.conv_store.get_conversation(session_id)
    assert seen is not None
    reading, release = threading.Event(), threading.Event()
    get_conversation = env.conv_store.get_conversation

    def first_read_waits(conversation_id: str) -> Conversation | None:
        if not reading.is_set():
            reading.set()
            release.wait(timeout=10)
        return get_conversation(conversation_id)

    monkeypatch.setattr(env.conv_store, "get_conversation", first_read_waits)
    admissions: list[str] = []

    class _Registry:
        async def admit_launch(self, conn: object, conversation_id: str, **_: object) -> None:
            admissions.append(conversation_id)

        def mcp_disabled_servers_active_harnesses(self) -> tuple[str, ...]:
            return ()

        def send_text(self, conn: object, text: str) -> None:
            # The runner is bound by now; the host's answer isn't under test.
            raise ConnectionError("host gone")

        def get(self, host_id: str) -> None:
            return None

    registry = cast(Any, _Registry())

    def host_conn() -> Any:
        return SimpleNamespace(host_id=_MOVED_SANDBOX_HOST_ID, pending_launches={})

    recovery = asyncio.create_task(
        _launch_runner_on_host_impl(seen, env.conv_store, registry, host_conn())
    )
    assert await asyncio.to_thread(reading.wait, 10)
    move = asyncio.create_task(
        _launch_runner_on_host_impl(
            seen, env.conv_store, registry, host_conn(), replace_move_claim=True
        )
    )
    await asyncio.sleep(0)
    release.set()
    refused, started = await asyncio.gather(recovery, move)

    assert refused.error_code == "session_binding_changed"
    assert started.error_code is None
    assert admissions == [session_id]
    conv = get_conversation(session_id)
    assert conv is not None
    assert conv.runner_id == started.runner_id
    assert not is_move_claim(conv.runner_id)


@pytest.mark.parametrize("runner_id", ["", "runner_other_tab"])
async def test_runner_change_waits_for_a_move_to_release_its_claim(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    runner_id: str,
) -> None:
    """
    Releasing or rebinding the runner of a session a move holds is refused, so
    nothing takes the session while the move settles. A claim past its lease,
    left by a server that stopped mid-move, can be cleared; the startup its move
    left pending is settled with it, so messages are dispatched normally again
    instead of queueing for a sandbox nothing will launch. The recovery holds the
    session while it settles, so another recovery or a new move waits for it.
    """
    import threading
    import time

    from starlette.datastructures import Headers

    from omnigent.server.managed_hosts import (
        MOVE_CLAIM_LEASE_S,
        MOVE_CLAIM_RUNNER_ID_PREFIX,
        new_move_claim,
    )
    from omnigent.server.schemas import SessionEventInput

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-claim-patch-agent")
    claim = new_move_claim()
    assert env.conv_store.set_runner_id(session_id, claim)

    refused = await env.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})

    assert refused.status_code == 409, refused.text
    assert "moving to a new sandbox" in refused.json()["error"]["message"]
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.runner_id == claim

    lapsed = (
        MOVE_CLAIM_RUNNER_ID_PREFIX
        + f"{int(time.time()) - MOVE_CLAIM_LEASE_S - 1:012x}"
        + "0" * 32
    )
    env.conv_store.replace_runner_id(session_id, lapsed)
    # The stopped move had marked its startup pending, still on the user's host,
    # and a message was held for its sandbox.
    env.conv_store.set_labels(session_id, {STARTUP_LABEL: "pending"})
    startup = env.app.state.startup_inputs
    event = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    )
    await startup.enqueue(session_id, event, user_id=None, headers=Headers({}), first=False)
    # The recovery holds the session while it settles: a second recovery and a
    # new move are both refused, so its settlement can't touch theirs.
    settling, settled = threading.Event(), threading.Event()
    list_pending_inputs = env.conv_store.list_pending_inputs

    def settle_slowly(conversation_id: str) -> Any:
        if not settling.is_set():
            settling.set()
            settled.wait(timeout=10)
        return list_pending_inputs(conversation_id)

    monkeypatch.setattr(env.conv_store, "list_pending_inputs", settle_slowly)
    recovery = asyncio.create_task(
        env.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": ""})
    )
    assert await asyncio.to_thread(settling.wait, 10)
    second = await env.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": ""})
    move = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
    settled.set()
    cleared = await recovery

    assert (second.status_code, move.status_code) == (409, 409), (second.text, move.text)
    assert cleared.status_code == 200, cleared.text
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.runner_id) == (_LAPTOP_HOST_ID, None)
    assert conv.labels.get(STARTUP_LABEL) == STARTUP_WITHDRAWN
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == ["failed"]
    assert not await startup.accepts(conv, env.app)


@pytest.mark.parametrize("runner_id", ["", "runner_other_tab"])
async def test_runner_release_racing_a_move_keeps_its_claim(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    runner_id: str,
) -> None:
    """
    A runner release or rebind that read the session just before a move claimed
    it is refused as a conflict rather than overwriting the move's claim.
    """
    from omnigent.server.managed_hosts import new_move_claim
    from tests.server.helpers import register_test_runner

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-claim-race-agent")
    claim = new_move_claim()
    method = "clear_runner_id" if runner_id == "" else "replace_runner_id"
    if runner_id:
        register_test_runner(env.app, runner_id)
    write = getattr(env.conv_store, method)

    def claim_then_write(conversation_id: str, *args: Any, **kwargs: Any) -> Conversation:
        assert env.conv_store.set_runner_id(conversation_id, claim)
        return write(conversation_id, *args, **kwargs)

    monkeypatch.setattr(env.conv_store, method, claim_then_write)

    resp = await env.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})

    assert resp.status_code == 409, resp.text
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.runner_id == claim


async def test_session_stays_with_a_refused_move_until_its_rollback_finishes(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    While a refused move rolls back, it still holds the session: a runner
    release and another move are both refused, so the rollback's cleanup can't
    land on a launch registered after it. The old binding then comes back.
    """
    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-rollback-hold-agent")
    loop = asyncio.get_running_loop()
    set_host_id = env.conv_store.set_host_id
    attempts: list[int] = []

    async def compete() -> None:
        release = await env.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": ""})
        move = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
        attempts.extend([release.status_code, move.status_code])

    def compete_then_restore(conversation_id: str, *args: object, **kwargs: object) -> object:
        if kwargs.get("expected_runner_id") is not None and not attempts:
            asyncio.run_coroutine_threadsafe(compete(), loop).result(timeout=10)
        return set_host_id(conversation_id, *args, **kwargs)

    monkeypatch.setattr(env.conv_store, "set_host_id", compete_then_restore)

    resp = await env.client.post(
        f"/v1/sessions/{session_id}/managed-host", json={"sandbox_provider": "e2b"}
    )

    assert resp.status_code == 400, resp.text
    assert attempts == [409, 409]
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.runner_id) == (
        _LAPTOP_HOST_ID,
        "/Users/alice/repo",
        None,
    )
    assert env.app.state.managed_launches.get(session_id) is None


@pytest.mark.parametrize(
    ("state", "settled_by", "runner", "expected"),
    [
        ("failed", "own", "own", "failed"),
        ("failed", "own", None, "failed"),
        ("failed", "earlier", "own", "in_progress"),
        ("failed", None, None, "in_progress"),
        (STARTUP_WITHDRAWN, "own", "own", "failed"),
    ],
)
async def test_message_saved_after_its_move_settled_settles_itself(
    managed_session_env: ManagedSessionEnv,
    state: str,
    settled_by: str | None,
    runner: str | None,
    expected: str,
) -> None:
    """
    A move settles while it holds the session, so a message admitted for it can
    be saved just after: it settles itself the same way, rather than waiting on
    a launch that's over. A later move's claim, before it marks its own startup
    pending, keeps the message; so does a create's failed launch.
    """
    from starlette.datastructures import Headers

    from omnigent.server.managed_hosts import new_move_claim
    from omnigent.server.schemas import SessionEventInput
    from omnigent.server.startup_inputs import STARTUP_SETTLED_BY_LABEL

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-late-input-agent")
    claims = {"own": new_move_claim(), "earlier": new_move_claim()}
    labels = {STARTUP_LABEL: state}
    if settled_by is not None:
        labels[STARTUP_SETTLED_BY_LABEL] = claims[settled_by]
    env.conv_store.set_labels(session_id, labels)
    if runner is not None:
        assert env.conv_store.set_runner_id(session_id, claims[runner])
    event = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    )

    await env.app.state.startup_inputs.enqueue(
        session_id, event, user_id=None, headers=Headers({}), first=False
    )

    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == [expected]


async def test_move_cancelled_during_its_detach_restores_the_whole_binding(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A move cancelled while detaching the old host waits for that write before
    rolling back, so the old host, directory and branch all come back and the
    claim is released, instead of the late detach clearing the restored host.
    """
    import threading

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-cancel-detach-agent")
    detaching, release = threading.Event(), threading.Event()
    clear_host_binding = env.conv_store.clear_host_binding

    def slow_detach(conversation_id: str, **kwargs: Any) -> Conversation:
        if kwargs.get("keep_runner"):
            detaching.set()
            release.wait(timeout=10)
        return clear_host_binding(conversation_id, **kwargs)

    monkeypatch.setattr(env.conv_store, "clear_host_binding", slow_detach)

    move = asyncio.create_task(env.client.post(f"/v1/sessions/{session_id}/managed-host", json={}))
    assert await asyncio.to_thread(detaching.wait, 10)
    move.cancel()
    release.set()
    await asyncio.gather(move, return_exceptions=True)

    def rolled_back() -> bool:
        current = env.conv_store.get_conversation(session_id)
        return current is not None and current.labels.get(STARTUP_LABEL) == STARTUP_WITHDRAWN

    await poll_until_async(rolled_back)
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.git_branch, conv.runner_id) == (
        _LAPTOP_HOST_ID,
        "/Users/alice/repo",
        "feature/x",
        None,
    )
    assert env.app.state.managed_launches.get(session_id) is None


async def test_relaunch_admitted_before_a_move_claims_keeps_the_claim(
    managed_session_env: ManagedSessionEnv,
) -> None:
    """
    A relaunch on the old host that read the session before the move claimed it,
    and was admitted while the move claimed and detached it, can't overwrite the
    claim: its runner replacement is checked against the runner it read.
    """
    from typing import cast

    from omnigent.server.routes._sessions.helpers import _launch_runner_on_host_locked

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-relaunch-race-agent")
    seen = env.conv_store.get_conversation(session_id)
    assert seen is not None and seen.runner_id is None
    claim = "runner_token_move_claim"

    class _AdmitWhileTheMoveClaims:
        async def admit_launch(self, conn: object, conversation_id: str, **_: object) -> None:
            assert env.conv_store.set_runner_id(conversation_id, claim)
            env.conv_store.clear_host_binding(
                conversation_id, expected_runner_id=claim, keep_runner=True
            )

    attempt = await _launch_runner_on_host_locked(
        seen,
        env.conv_store,
        cast(Any, _AdmitWhileTheMoveClaims()),
        cast(Any, SimpleNamespace(host_id=_LAPTOP_HOST_ID)),
    )

    assert attempt.error_code == "session_binding_changed"
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.runner_id) == (None, claim)


async def test_message_after_the_move_claims_waits_for_the_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A message that arrives as soon as the move claims the session, before it
    writes anything else and with the old host still bound and offline, is held
    for the sandbox. An earlier refused move's withdrawn startup doesn't settle
    it as not sent.
    """
    import threading

    from omnigent.server.managed_hosts import is_move_claim, new_move_claim
    from omnigent.server.startup_inputs import STARTUP_SETTLED_BY_LABEL

    env = managed_session_env
    gate = threading.Event()
    install_fake_modal_launcher(
        monkeypatch, FakeSandboxLauncher(provision_gate=gate, fail_on_host_start=True)
    )
    session_id = await _session_released_from_laptop(env, agent_name="move-claim-message-agent")
    env.conv_store.set_labels(
        session_id,
        {STARTUP_LABEL: STARTUP_WITHDRAWN, STARTUP_SETTLED_BY_LABEL: new_move_claim()},
    )
    env.host_store.set_offline(_LAPTOP_HOST_ID)
    loop = asyncio.get_running_loop()
    replace_runner_id = env.conv_store.replace_runner_id
    responses: list[dict[str, Any]] = []

    def claim_then_message(conversation_id: str, runner_id: str, **kwargs: Any) -> Conversation:
        claimed = replace_runner_id(conversation_id, runner_id, **kwargs)
        if is_move_claim(runner_id) and not responses:
            responses.append(
                asyncio.run_coroutine_threadsafe(
                    _post_user_message(env, conversation_id), loop
                ).result(timeout=10)
            )
        return claimed

    monkeypatch.setattr(env.conv_store, "replace_runner_id", claim_then_message)

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})

    assert resp.status_code == 202, resp.text
    assert len(responses) == 1 and responses[0]["queued"] is True
    assert responses[0].get("pending_id")
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == [
        "in_progress"
    ]
    gate.set()
    await poll_until_async(
        lambda: (
            [item.status for item in env.conv_store.list_pending_inputs(session_id)] == ["failed"]
        )
    )


@pytest.mark.parametrize("winner_state", ["sandbox runner bound", "claim released on handover"])
async def test_delayed_sandbox_move_leaves_the_winners_startup_alone(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    winner_state: str,
) -> None:
    """
    A move that read the session before another move won it, and claims only
    once the winner's sandbox is bound (its runner in place, or its claim
    released for another replica to finish), is refused without touching the
    winner's startup or the message held for it.
    """
    from starlette.datastructures import Headers

    from omnigent.server.managed_hosts import is_move_claim
    from omnigent.server.schemas import SessionEventInput

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-delayed-loser-agent")
    startup = env.app.state.startup_inputs
    loop = asyncio.get_running_loop()
    replace_runner_id = env.conv_store.replace_runner_id
    won: list[bool] = []

    async def win() -> None:
        store = env.conv_store
        await asyncio.to_thread(
            store.set_host_id, session_id, _MOVED_SANDBOX_HOST_ID, "/workspace"
        )
        if winner_state == "sandbox runner bound":
            await asyncio.to_thread(store.set_runner_id, session_id, "runner_sandbox")
        await asyncio.to_thread(store.set_labels, session_id, {STARTUP_LABEL: "pending"})
        event = SessionEventInput(
            type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        )
        await startup.enqueue(session_id, event, user_id=None, headers=Headers({}), first=False)

    def win_then_claim(conversation_id: str, runner_id: str, **kwargs: Any) -> Conversation:
        if is_move_claim(runner_id) and not won:
            won.append(True)
            asyncio.run_coroutine_threadsafe(win(), loop).result(timeout=10)
        return replace_runner_id(conversation_id, runner_id, **kwargs)

    monkeypatch.setattr(env.conv_store, "replace_runner_id", win_then_claim)

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})

    assert resp.status_code == 409, resp.text
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.runner_id) == (
        _MOVED_SANDBOX_HOST_ID,
        "runner_sandbox" if winner_state == "sandbox runner bound" else None,
    )
    assert conv.labels.get(STARTUP_LABEL) == "pending"
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == [
        "in_progress"
    ]
    assert env.app.state.managed_launches.get(session_id) is None


async def test_move_cancelled_during_its_startup_write_leaves_nothing_held(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A move cancelled while its pending-startup write is in flight waits for the
    write and then withdraws it, so the restored session, even with its host
    offline, doesn't go on holding messages that nothing would deliver.
    """
    import threading

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-cancel-startup-agent")
    env.host_store.set_offline(_LAPTOP_HOST_ID)
    writing, release = threading.Event(), threading.Event()
    set_labels = env.conv_store.set_labels

    def slow_startup_write(conversation_id: str, labels: dict[str, str]) -> object:
        if labels.get(STARTUP_LABEL) == "pending":
            writing.set()
            release.wait(timeout=10)
        return set_labels(conversation_id, labels)

    monkeypatch.setattr(env.conv_store, "set_labels", slow_startup_write)

    move = asyncio.create_task(env.client.post(f"/v1/sessions/{session_id}/managed-host", json={}))
    assert await asyncio.to_thread(writing.wait, 10)
    move.cancel()
    release.set()
    await asyncio.gather(move, return_exceptions=True)

    # The rollback finishes after the cancelled request.
    await poll_until_async(
        lambda: (
            (current := env.conv_store.get_conversation(session_id)) is not None
            and current.labels.get(STARTUP_LABEL) == STARTUP_WITHDRAWN
        )
    )
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert conv.labels.get(STARTUP_LABEL) == STARTUP_WITHDRAWN
    assert (conv.host_id, conv.runner_id) == (_LAPTOP_HOST_ID, None)
    assert env.conv_store.list_pending_inputs(session_id) == []
    startup = env.app.state.startup_inputs
    assert not await startup.accepts(conv, env.app)


async def test_message_admitted_while_a_launch_is_withdrawn_is_settled(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A message admitted behind a pending startup that lands after the withdrawal
    took its snapshot settles itself on seeing the withdrawn startup, through the
    real enqueue path, instead of staying queued with nothing to deliver it.
    """
    from starlette.datastructures import Headers

    from omnigent.server.schemas import SessionEventInput

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-withdraw-race-agent")
    env.conv_store.set_labels(session_id, {STARTUP_LABEL: "pending"})
    startup = env.app.state.startup_inputs
    loop = asyncio.get_running_loop()
    list_pending_inputs = env.conv_store.list_pending_inputs
    raced: list[object] = []

    def snapshot_then_message(conversation_id: str) -> Any:
        snapshot = list_pending_inputs(conversation_id)
        if not raced:
            event = SessionEventInput(
                type="message",
                data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            )
            raced.append(
                asyncio.run_coroutine_threadsafe(
                    startup.enqueue(
                        conversation_id, event, user_id=None, headers=Headers({}), first=False
                    ),
                    loop,
                ).result(timeout=10)
            )
        return snapshot

    monkeypatch.setattr(env.conv_store, "list_pending_inputs", snapshot_then_message)

    await startup.release_held_inputs(session_id, "The message wasn't sent.", move_claim=None)

    assert len(raced) == 1
    assert [item.status for item in list_pending_inputs(session_id)] == ["failed"]
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.labels.get(STARTUP_LABEL) == STARTUP_WITHDRAWN


async def test_shutdown_after_the_sandbox_runner_starts_keeps_held_messages(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Shutdown cancelling a move after its sandbox runner replaced the claim, while
    the launch waits for that runner's tunnel, leaves the sandbox binding and the
    held message for the sandbox's replica to finish, rather than treating the
    move's own runner as another host winning.
    """
    import threading

    from omnigent.server.routes.sessions import cancel_managed_launch_tasks

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    start, host_futures = _fake_host_connector(env, asyncio.get_running_loop())
    provisioned = threading.Event()
    fake = FakeSandboxLauncher(on_host_start=start, provision_gate=provisioned)
    install_fake_modal_launcher(monkeypatch, fake)
    waiting = asyncio.Event()

    async def runner_never_connects(_runner_id: str, *, timeout_s: float) -> object:
        waiting.set()
        await asyncio.Event().wait()
        return None

    monkeypatch.setattr(env.app.state.tunnel_registry, "wait_for_runner", runner_never_connects)
    session_id = await _session_released_from_laptop(env, agent_name="move-shutdown-runner-agent")

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
    assert resp.status_code == 202, resp.text
    moving = env.conv_store.get_conversation(session_id)
    assert moving is not None and moving.runner_id is not None
    held = await _post_user_message(env, session_id)
    assert held["queued"] is True
    provisioned.set()
    await asyncio.wait_for(waiting.wait(), timeout=15)

    await cancel_managed_launch_tasks(drain_s=0)

    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert conv.host_id is not None and conv.host_id != _LAPTOP_HOST_ID
    assert conv.runner_id not in (None, moving.runner_id)
    assert conv.labels.get(STARTUP_LABEL) == "pending"
    assert [item.status for item in env.conv_store.list_pending_inputs(session_id)] == [
        "in_progress"
    ]
    for future in host_futures:
        future.cancel()


async def test_sandbox_bound_on_another_replica_hands_over_without_the_claim(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    When the sandbox host is online in shared storage but its tunnel is on
    another replica, the launching replica binds the session and drops the
    move's claim, so that replica's startup recovery can start the runner. A
    recovery pass there between the bind and the handover waits for the claim
    instead of finishing the startup, and the next one starts the runner with
    no message needed.
    """
    import threading

    from omnigent.server.managed_hosts import is_move_claim

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    started: list[str] = []

    def online_elsewhere(invocation: HostStartInvocation) -> None:
        # Registered and live in shared storage; no tunnel on this replica.
        env.host_store.upsert_on_connect(
            invocation.host_id, invocation.host_name, RESERVED_USER_LOCAL
        )
        started.append(invocation.host_id)

    # The replica holding the sandbox's tunnel, where its startup recovery runs.
    other_app, _, _ = _managed_app(db_uri, tmp_path / "sandbox-replica")
    other_startup = other_app.state.startup_inputs
    other_hosts = other_startup.hosts
    admissions: list[str] = []

    async def admit_launch(conn: object, conversation_id: str, **_: object) -> None:
        admissions.append(conversation_id)

    def host_gone(conn: object, text: str) -> None:
        # The runner is bound by now; the host's answer isn't under test.
        raise ConnectionError("host gone")

    monkeypatch.setattr(other_hosts, "admit_launch", admit_launch)
    monkeypatch.setattr(other_hosts, "send_text", host_gone)
    monkeypatch.setattr(other_hosts, "get", lambda host_id: None)

    def sandbox_connection(host_id: str) -> Any:
        return SimpleNamespace(
            host_id=host_id, owner=None, pending_launches={}, registered_with_managed_token=True
        )

    loop = asyncio.get_running_loop()
    early_recovery: list[bool] = []
    clear_runner_id = env.conv_store.clear_runner_id

    def recover_there_then_hand_over(conversation_id: str, **kwargs: Any) -> Conversation:
        expected = kwargs.get("expected_binding")
        if (
            expected is not None
            and is_move_claim(expected.runner_id)
            and expected.host_id is not None
            and not early_recovery
        ):
            early_recovery.append(
                asyncio.run_coroutine_threadsafe(
                    other_startup._recover_session(
                        other_app, conversation_id, sandbox_connection(expected.host_id)
                    ),
                    loop,
                ).result(timeout=10)
            )
        return clear_runner_id(conversation_id, **kwargs)

    monkeypatch.setattr(env.conv_store, "clear_runner_id", recover_there_then_hand_over)
    provisioned = threading.Event()
    fake = FakeSandboxLauncher(on_host_start=online_elsewhere, provision_gate=provisioned)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-cross-replica-agent")

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
    assert resp.status_code == 202, resp.text
    provisioned.set()

    tracker = env.app.state.managed_launches
    await poll_until_async(lambda: started and tracker.get(session_id) is None)
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.runner_id) == (started[0], None)
    assert conv.labels.get(STARTUP_LABEL) == "pending"
    # Still in flight, so the worker polls again soon.
    assert early_recovery == [True]
    assert admissions == []

    assert await other_startup._recover_session(
        other_app, session_id, sandbox_connection(started[0])
    )
    assert admissions == [session_id]
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.runner_id is not None
    assert not is_move_claim(conv.runner_id)


async def test_refused_sandbox_move_releases_its_claim_when_restoring_labels_fails(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A refused move releases its claim even when putting the earlier sandbox's
    labels back fails, so the session isn't left with a runner nothing holds.
    """
    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-label-restore-agent")
    env.conv_store.set_labels(session_id, _STALE_SANDBOX_LABELS)
    set_labels = env.conv_store.set_labels

    def fail_restoring(conversation_id: str, labels: dict[str, str]) -> object:
        if labels == _STALE_SANDBOX_LABELS:
            raise RuntimeError("label store unavailable")
        return set_labels(conversation_id, labels)

    monkeypatch.setattr(env.conv_store, "set_labels", fail_restoring)

    resp = await env.client.post(
        f"/v1/sessions/{session_id}/managed-host", json={"sandbox_provider": "e2b"}
    )

    assert resp.status_code == 400, resp.text
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.runner_id) == (_LAPTOP_HOST_ID, None)


async def test_message_on_another_replica_before_the_move_detaches_waits_for_the_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    A message reaching another replica after the move registered its launch but
    before it detached the old host is held for the sandbox, even though that
    replica has no launch tracker and the old host is offline: the pending
    startup on a user-connected host is what marks the move, in shared storage.
    """
    env = managed_session_env
    fake = FakeSandboxLauncher(fail_on_host_start=True)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-replica-message-agent")
    env.host_store.set_offline(_LAPTOP_HOST_ID)
    other_app, _, _ = _managed_app(db_uri, tmp_path / "replica-a")
    loop = asyncio.get_running_loop()
    clear_host_binding = env.conv_store.clear_host_binding
    responses: list[dict[str, Any]] = []

    async def message_via_other_replica() -> dict[str, Any]:
        async with AsyncClient(
            transport=ASGITransport(app=other_app), base_url="http://test"
        ) as other:
            resp = await asyncio.wait_for(
                other.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}],
                        },
                    },
                ),
                timeout=10,
            )
            assert resp.status_code == 202, resp.text
            return resp.json()

    def message_then_detach(conversation_id: str, **kwargs: Any) -> Conversation:
        if kwargs.get("keep_runner"):
            current = env.conv_store.get_conversation(conversation_id)
            assert current is not None and current.host_id == _LAPTOP_HOST_ID
            responses.append(
                asyncio.run_coroutine_threadsafe(message_via_other_replica(), loop).result(
                    timeout=15
                )
            )
        return clear_host_binding(conversation_id, **kwargs)

    monkeypatch.setattr(env.conv_store, "clear_host_binding", message_then_detach)

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})

    assert resp.status_code == 202, resp.text
    assert len(responses) == 1 and responses[0]["queued"] is True
    assert responses[0].get("pending_id")
    await poll_until_async(
        lambda: (
            [item.status for item in env.conv_store.list_pending_inputs(session_id)] == ["failed"]
        )
    )


async def test_message_sent_as_the_move_detaches_waits_for_the_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The launch is visible to message handling before the old host is detached,
    so a message that arrives right as the session leaves its host is saved for
    the sandbox rather than dropped. When the launch then fails, the message is
    reported as not delivered.
    """
    env = managed_session_env
    fake = FakeSandboxLauncher(fail_on_host_start=True)
    install_fake_modal_launcher(monkeypatch, fake)
    session_id = await _session_released_from_laptop(env, agent_name="move-detach-message-agent")
    loop = asyncio.get_running_loop()
    clear_host_binding = env.conv_store.clear_host_binding
    responses: list[dict[str, Any]] = []

    def detach_then_message(conversation_id: str, **kwargs: Any) -> Conversation:
        conv = clear_host_binding(conversation_id, **kwargs)
        if kwargs.get("keep_runner"):
            responses.append(
                asyncio.run_coroutine_threadsafe(
                    _post_user_message(env, conversation_id), loop
                ).result(timeout=10)
            )
        return conv

    monkeypatch.setattr(env.conv_store, "clear_host_binding", detach_then_message)

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})

    assert resp.status_code == 202, resp.text
    assert len(responses) == 1 and responses[0]["queued"] is True
    assert responses[0].get("pending_id")
    await poll_until_async(
        lambda: (
            [item.status for item in env.conv_store.list_pending_inputs(session_id)] == ["failed"]
        )
    )
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.runner_id) == (None, None)


async def test_rebind_as_a_refused_sandbox_move_rolls_back_keeps_the_winner(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A refused move puts the old host back only while its claim still holds the
    session, checked with the write. A host that took the session after the
    claim was released, just as the rollback writes, keeps its binding and
    runner.
    """
    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-rollback-race-agent")
    other_host_id = "99999999999999999999999999999999"
    env.host_store.upsert_on_connect(other_host_id, "desktop", RESERVED_USER_LOCAL)
    set_host_id = env.conv_store.set_host_id
    rebound = False

    def set_host_id_after_a_rebind(
        conversation_id: str, *args: object, **kwargs: object
    ) -> object:
        nonlocal rebound
        if not rebound:
            rebound = True
            # Only after the move's claim is released can another host bind.
            env.conv_store.clear_runner_id(conversation_id)
            assert env.conv_store.set_runner_id(conversation_id, "runner_desktop")
            set_host_id(conversation_id, other_host_id, "/Users/alice/desktop")
        return set_host_id(conversation_id, *args, **kwargs)

    monkeypatch.setattr(env.conv_store, "set_host_id", set_host_id_after_a_rebind)

    resp = await env.client.post(
        f"/v1/sessions/{session_id}/managed-host", json={"sandbox_provider": "e2b"}
    )

    assert resp.status_code == 400, resp.text
    assert rebound
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.runner_id) == (
        other_host_id,
        "/Users/alice/desktop",
        "runner_desktop",
    )
    assert env.app.state.managed_launches.get(session_id) is None


@pytest.mark.parametrize(
    "failing_step", ["after claim", "startup write", "host clear", "after host clear"]
)
async def test_sandbox_move_failing_right_after_its_claim_releases_it(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    """
    A store error between claiming the session and clearing its host releases
    the claim, so the session stays on its host and a later move or launch
    isn't refused for a runner nothing holds.
    """
    from omnigent.server.managed_hosts import is_move_claim

    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-claim-leak-agent")
    env.conv_store.set_labels(session_id, _STALE_SANDBOX_LABELS)
    get_conversation = env.conv_store.get_conversation
    replace_runner_id = env.conv_store.replace_runner_id
    set_labels = env.conv_store.set_labels
    clear_host_binding = env.conv_store.clear_host_binding

    def claim(conversation_id: str, runner_id: str, **kwargs: Any) -> Conversation:
        claimed = replace_runner_id(conversation_id, runner_id, **kwargs)
        if failing_step == "after claim" and is_move_claim(runner_id):
            raise RuntimeError("store unavailable")
        return claimed

    def label(conversation_id: str, labels: dict[str, str]) -> object:
        if failing_step == "startup write" and labels.get(STARTUP_LABEL) == "pending":
            raise RuntimeError("store unavailable")
        return set_labels(conversation_id, labels)

    def clear(conversation_id: str, **kwargs: object) -> Conversation:
        if failing_step == "host clear":
            raise RuntimeError("store unavailable")
        cleared = clear_host_binding(conversation_id, **kwargs)
        if failing_step == "after host clear":
            raise RuntimeError("store unavailable")
        return cleared

    monkeypatch.setattr(env.conv_store, "replace_runner_id", claim)
    monkeypatch.setattr(env.conv_store, "set_labels", label)
    monkeypatch.setattr(env.conv_store, "clear_host_binding", clear)

    with pytest.raises(RuntimeError, match="store unavailable"):
        await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})

    conv = get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.git_branch, conv.runner_id) == (
        _LAPTOP_HOST_ID,
        "/Users/alice/repo",
        "feature/x",
        None,
    )
    # Whether or not the move read them, the earlier sandbox's labels survive.
    assert {key: conv.labels.get(key) for key in _STALE_SANDBOX_LABELS} == _STALE_SANDBOX_LABELS
    assert env.app.state.managed_launches.get(session_id) is None


async def test_refused_sandbox_move_restores_the_session_binding(
    managed_session_env: ManagedSessionEnv,
) -> None:
    """
    A move the server can't launch (here, an unconfigured provider) fails the
    POST and puts the old host, directory, branch and sandbox labels back, so
    the session is exactly where the runner release left it.
    """
    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-refused-agent")
    env.conv_store.set_labels(session_id, _STALE_SANDBOX_LABELS)

    resp = await env.client.post(
        f"/v1/sessions/{session_id}/managed-host", json={"sandbox_provider": "e2b"}
    )

    assert resp.status_code == 400, resp.text
    assert "not configured" in resp.json()["error"]["message"]
    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.git_branch, conv.runner_id) == (
        _LAPTOP_HOST_ID,
        "/Users/alice/repo",
        "feature/x",
        None,
    )
    assert {key: conv.labels.get(key) for key in _STALE_SANDBOX_LABELS} == _STALE_SANDBOX_LABELS
    assert env.app.state.managed_launches.get(session_id) is None


async def test_sandbox_move_failing_partway_restores_the_session_binding(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A store failure partway through the move, here while removing the earlier
    sandbox's labels after the claim and before the old host is detached, still
    puts the old binding and labels back.
    """
    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-partway-agent")
    env.conv_store.set_labels(session_id, _STALE_SANDBOX_LABELS)
    delete_label = env.conv_store.delete_label
    deleted: list[str] = []

    def delete_one_label_then_fail(conversation_id: str, key: str) -> None:
        if deleted:
            raise RuntimeError("label store unavailable")
        deleted.append(key)
        delete_label(conversation_id, key)

    monkeypatch.setattr(env.conv_store, "delete_label", delete_one_label_then_fail)

    with pytest.raises(RuntimeError, match="label store unavailable"):
        await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})

    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None
    assert (conv.host_id, conv.workspace, conv.git_branch, conv.runner_id) == (
        _LAPTOP_HOST_ID,
        "/Users/alice/repo",
        "feature/x",
        None,
    )
    assert {key: conv.labels.get(key) for key in _STALE_SANDBOX_LABELS} == _STALE_SANDBOX_LABELS
    assert env.app.state.managed_launches.get(session_id) is None


@pytest.mark.parametrize(
    ("reason", "expected_status"),
    [
        ("runner still bound", 409),
        ("no host to move from", 409),
        ("requested host id taken", 409),
        ("side chat", 400),
        ("already in a sandbox", 400),
        ("provider manages sandbox profiles", 400),
        ("nested agent limited to its host", 403),
    ],
)
async def test_sandbox_move_refuses_without_touching_the_session(
    managed_session_env: ManagedSessionEnv, reason: str, expected_status: int
) -> None:
    """
    A session whose runner was never released, that has no host to leave, or
    that is a side chat, or a move naming a taken host id, is refused without
    touching the session. So is one already in a sandbox nothing would tear
    down, a move onto a provider that binds a sandbox profile at create, and a
    nested agent the server limits to its current host.
    """
    env = managed_session_env
    session_id = await _session_released_from_laptop(env, agent_name="move-guard-agent")
    request: dict[str, str] = {}
    if reason == "runner still bound":
        assert env.conv_store.set_runner_id(session_id, "runner_laptop")
    elif reason == "no host to move from":
        env.conv_store.clear_host_binding(session_id)
    elif reason == "side chat":
        env.conv_store.set_labels(session_id, {SIDE_CHAT_LABEL_KEY: "1"})
    elif reason == "requested host id taken":
        env.host_store.register_managed_host(
            host_id=_MOVED_SANDBOX_HOST_ID,
            name="managed-01234567",
            user_id=RESERVED_USER_LOCAL,
            token="tok-taken",
            provider="modal",
            sandbox_id="sb-taken",
            token_expires_at=9_999_999_999,
        )
        request = {"host_id": _MOVED_SANDBOX_HOST_ID}
    elif reason == "nested agent limited to its host":
        source = env.conv_store.create_conversation(agent_id=None)
        env.conv_store.add_new_sibling(source.id, session_id, user_id=None)
        env.app.state.feature_flags = FeatureFlags(
            enabled_features=env.app.state.feature_flags.enabled_features
            | {Feature.SIBLING_SESSIONS}
        )
        env.app.state.sibling_session_destinations = lambda: (
            SiblingSessionDestinations.CURRENT_HOST
        )
    elif reason == "provider manages sandbox profiles":
        deployment = env.app.state.sandbox_config
        env.app.state.sandbox_config = replace(
            deployment,
            configs=tuple(replace(config, manages_profiles=True) for config in deployment.configs),
        )
    else:
        env.host_store.register_managed_host(
            host_id=_MOVED_SANDBOX_HOST_ID,
            name="managed-01234567",
            user_id=RESERVED_USER_LOCAL,
            token="tok-current",
            provider="modal",
            sandbox_id="sb-current",
            token_expires_at=9_999_999_999,
        )
        env.conv_store.set_host_id(session_id, _MOVED_SANDBOX_HOST_ID, "/root/workspace")
    before = env.conv_store.get_conversation(session_id)

    resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json=request)

    assert resp.status_code == expected_status, resp.text
    assert env.conv_store.get_conversation(session_id) == before
    assert env.app.state.managed_launches.get(session_id) is None


async def test_managed_launch_progress_surfaces_on_snapshot_and_stream(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A managed launch reports live progress: the snapshot carries the
    seeded ``provisioning`` stage from the moment the create returns
    (the Web UI navigates to the session page immediately and
    cold-loads the snapshot), the live stream then sees every later
    stage in pipeline order — cloning → starting → connecting →
    ready — and a settled launch clears the snapshot field.

    The fake launcher holds ``provision`` on a gate so the stream
    subscription registers deterministically before any
    post-provision stage fires; without the gate the launch could
    race past ``cloning`` before the subscriber attaches (the stream
    has no replay).
    """
    import threading

    from omnigent.runtime import session_stream

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    # This test asserts the happy-path "ready" progress edge; fake the
    # runner tunnel connect so settlement is driven by progress ordering,
    # not by the runner WebSocket harness.
    monkeypatch.setattr(
        env.app.state.tunnel_registry,
        "wait_for_runner",
        lambda _runner_id, *, timeout_s: asyncio.sleep(0, result=object()),
    )
    loop = asyncio.get_running_loop()
    gate = threading.Event()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it."""
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(provision_gate=gate, on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-progress-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_type": "managed",
            "workspace": "https://github.com/org/myrepo.git#main",
        },
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    # The create route seeds the progress cache before scheduling the
    # background task, so the snapshot a navigating client cold-loads
    # already carries the stage. A regression here reverts the
    # session page to a silent dead chat during provisioning.
    snapshot = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["sandbox_status"] == {
        "stage": "provisioning",
        "error": None,
        "provider": "modal",
        "details": None,
    }

    # Tail live progress at the layer the SSE endpoint reads it from.
    # (The endpoint itself never terminates under the in-process ASGI
    # transport — see the stream test in test_sessions_permissions.)
    stages: list[str] = []
    providers: set[str | None] = set()
    subscribed = asyncio.Event()
    settled = asyncio.Event()

    async def _tail_stages() -> None:
        """Collect sandbox_status stages until a terminal one lands."""
        async for event in session_stream.subscribe(
            session_id, ready_event={"type": "_test_subscribed"}
        ):
            if event.get("type") == "_test_subscribed":
                subscribed.set()
                continue
            if event.get("type") != "session.sandbox_status":
                continue
            stages.append(event["stage"])
            providers.add(event["provider"])
            if event["stage"] in ("ready", "failed"):
                settled.set()
                return

    tail_task = asyncio.create_task(_tail_stages())
    try:
        # The subscriber slot must be live before the gate releases,
        # or early stages could be lost (live-tail only, no replay).
        await asyncio.wait_for(subscribed.wait(), timeout=5.0)
        gate.set()
        await asyncio.wait_for(settled.wait(), timeout=15.0)
    finally:
        tail_task.cancel()
    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1

    # Every post-provision stage, in pipeline order. "provisioning"
    # itself predates the subscription (seeded at create; covered by
    # the snapshot assert above). A missing "cloning" means the
    # worker-thread emission was dropped; missing "connecting"/"ready"
    # means the background task stopped reporting after the host
    # bound; "failed" here means the launch itself broke.
    assert stages == ["cloning", "starting", "connecting", "ready"]
    # Later stages keep the provider seeded at create, so the web UI keeps naming it.
    assert providers == {"modal"}

    # A settled launch clears the snapshot field — from here the
    # session looks like any host-bound session and a reloading
    # client sees no provisioning indicator.
    snapshot_after = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot_after.status_code == 200, snapshot_after.text
    assert snapshot_after.json()["sandbox_status"] is None
    del tunnels


async def test_managed_launch_action_required_reaches_snapshot_and_stream(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A launch that needs the user to act reports ``action_required`` with
    what to do: the live stream and the snapshot carry those details until
    the launch moves on.
    """
    from fastapi import HTTPException

    from omnigent.runtime import session_stream
    from omnigent.server.managed_hosts import SandboxStageObserver

    env = managed_session_env
    details = "Refresh your dbcert on your laptop."
    subscribed = asyncio.Event()
    finish = asyncio.Event()

    async def _launch_needing_action(*, on_stage: SandboxStageObserver, **_: object) -> None:
        """Stand in for a deployment's launch that finds the user has to act."""
        await subscribed.wait()
        # Details only belong to action_required; the publisher drops them elsewhere.
        on_stage("provisioning", "Not shown for this stage.")
        on_stage("action_required", details)
        await finish.wait()
        raise HTTPException(status_code=502, detail="managed sandbox launch failed: test over")

    monkeypatch.setattr(
        "omnigent.server.managed_hosts.launch_managed_host", _launch_needing_action
    )
    agent = await create_test_agent(env.client, name="managed-action-required-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    reports: list[dict[str, Any]] = []

    async def _tail_reports() -> None:
        """Collect sandbox_status events after subscribing, through the first action_required."""
        async for event in session_stream.subscribe(
            session_id, ready_event={"type": "_test_subscribed"}
        ):
            if event.get("type") == "_test_subscribed":
                subscribed.set()
            elif event.get("type") == "session.sandbox_status":
                reports.append(event)
                if event["stage"] == "action_required":
                    return

    try:
        await asyncio.wait_for(_tail_reports(), timeout=10.0)
        snapshot = await env.client.get(f"/v1/sessions/{session_id}")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["sandbox_status"] == {
            "stage": "action_required",
            "error": None,
            "provider": "modal",
            "details": details,
        }
    finally:
        finish.set()
    assert [(r["stage"], r["details"]) for r in reports] == [
        ("provisioning", None),
        ("action_required", details),
    ]

    # Let the stand-in launch settle as failed so no launch task outlives the test.
    deadline = asyncio.get_running_loop().time() + 10.0
    while asyncio.get_running_loop().time() < deadline:
        settled = await env.client.get(f"/v1/sessions/{session_id}")
        if (settled.json()["sandbox_status"] or {}).get("stage") == "failed":
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("the stand-in launch never settled as failed")
    # Each report replaces the last, so the failure no longer carries the details.
    assert settled.json()["sandbox_status"]["details"] is None


async def test_subagent_session_reuses_managed_sandbox_runner(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A child session of a managed session runs IN the parent's sandbox.

    Sub-agent spawns (the ``sys_session_create`` / spawn tools POST
    ``/v1/sessions`` with ``parent_session_id`` set, from inside the
    runner) must inherit the parent's runner — the process living in
    the managed sandbox — and must NOT provision a second sandbox or
    bind a host of their own. A second provision here would mean every
    sub-agent costs a sandbox; a missing runner inheritance would mean
    the sub-agent runs nowhere.
    """
    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it."""
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-subagent-parent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]
    parent = await _wait_for_managed_binding(env, parent_id)
    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1
    assert parent.runner_id is not None

    # The shape the in-runner spawn tools POST: same-server create
    # with parent_session_id (default external host_type, no host_id).
    child_resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "parent_session_id": parent_id},
    )
    assert child_resp.status_code == 201, child_resp.text
    child_id = child_resp.json()["id"]
    child = env.conv_store.get_conversation(child_id)
    assert child is not None

    # Sandbox reuse: the child rides the parent's runner — the process
    # inside the managed sandbox. A None here means the sub-agent has
    # no executor; a different id means it ran outside the sandbox.
    assert child.runner_id == parent.runner_id
    # No second sandbox was provisioned for the child, and the child
    # carries no host binding of its own (deleting the child must not
    # tear the parent's sandbox down — that cleanup keys on host_id).
    assert len(fake.provisioned_names) == 1
    assert child.host_id is None
    del tunnels


@pytest.mark.parametrize("requested_host_id", [None, "0123456789abcdef0123456789abcdef"])
async def test_message_relaunches_dead_managed_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    requested_host_id: str | None,
) -> None:
    """
    A message to a session whose managed sandbox died provisions a new
    sandbox GENERATION under the same host identity.

    Golden create → fake host registers → drop its tunnel (clean
    close, marking the row offline — the shape a dying sandbox leaves
    behind) → post a message. The message-dispatch relaunch path must
    detect the dead managed sandbox and run the relaunch pipeline:
    terminate the old generation, provision a fresh sandbox, re-arm
    the SAME host row with a new token + sandbox id, and re-bind the
    session's runner. Without the relaunch path, the message simply
    503s and none of the generation-2 effects below happen.
    """
    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    # Let the initial create settle successfully; after that this test switches
    # the relaunch generation back to a runner-connect timeout.
    monkeypatch.setattr(
        env.app.state.tunnel_registry,
        "wait_for_runner",
        lambda _runner_id, *, timeout_s: asyncio.sleep(0, result=object()),
    )
    # Shrink relaunch waits so the message POST and background task settle fast.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0.1)
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it."""
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-relaunch-agent")
    create: dict[str, object] = {"agent_id": agent["id"], "host_type": "managed"}
    if requested_host_id is not None:
        create["host_id"] = requested_host_id
    resp = await env.client.post("/v1/sessions", json=create)
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    conv = await _wait_for_managed_binding(env, session_id)
    assert conv.host_id is not None
    gen1_runner_id = conv.runner_id
    first_tunnel = await host_futures[0]

    # Wait for the CREATE launch to fully settle (tracker entry
    # popped). Disconnecting earlier makes the message below
    # rendezvous on the still-inflight create entry instead of
    # exercising the relaunch path under test.
    tracker = env.app.state.managed_launches
    deadline = loop.time() + 10.0
    while loop.time() < deadline and tracker.get(session_id) is not None:
        await asyncio.sleep(0.05)
    assert tracker.get(session_id) is None, "create launch never settled"

    monkeypatch.setattr(
        env.app.state.tunnel_registry,
        "wait_for_runner",
        lambda _runner_id, *, timeout_s: asyncio.sleep(0, result=None),
    )

    # Kill generation 1: a clean tunnel close marks the host row
    # offline — the state a dead sandbox leaves. Wait until both the
    # live registry and the row agree the host is gone, so the message
    # below deterministically takes the dead-host branch.
    await first_tunnel.send_input({"type": "websocket.disconnect", "code": 1000})
    host_registry = env.app.state.host_registry
    deadline = loop.time() + 10.0
    while loop.time() < deadline and (
        host_registry.get(conv.host_id) is not None or env.host_store.is_online(conv.host_id)
    ):
        await asyncio.sleep(0.05)
    assert host_registry.get(conv.host_id) is None
    assert env.host_store.is_online(conv.host_id) is False

    message_resp = await env.client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "wake up"}],
            },
        },
        # The POST rides the relaunch (provision + host registration);
        # generous bound, resolves in ~a second with the fake.
        timeout=30.0,
    )
    # No real runner ever connects in this harness, so the POST itself
    # ends 503 after the relaunch settles — the relaunch EFFECTS below
    # are the assertions that matter (with a real sandbox the runner
    # connects and the message forwards; covered by live verification).
    assert message_resp.status_code == 503, message_resp.text

    # Generation 2 was provisioned and the old generation terminated.
    assert len(fake.provisioned_names) == 2, (
        f"expected a second provision for the relaunch, got "
        f"{fake.provisioned_names} — the dead managed sandbox was not relaunched"
    )
    assert fake.terminated == ["sb-fake-1"]
    # The SAME host row carries the new generation: identity stable,
    # sandbox id rolled. A new host_id here means the relaunch minted a
    # fresh host instead of preserving the session's binding.
    host = env.host_store.get_host(conv.host_id)
    assert host is not None
    assert host.sandbox_id == "sb-fake-2"
    # The relaunched host registered over the tunnel with its NEW
    # token (the second start invocation), proving the re-armed
    # credential authenticates.
    assert len(fake.host_starts) == 2
    assert fake.host_starts[1].host_id == conv.host_id
    assert fake.host_starts[1].token != fake.host_starts[0].token
    # The relaunch keeps the keying the session's create chose.
    slice_key = render_host_config_write_command({"host": {"slice_key_enabled": True}})
    assert fake.commands.count(slice_key) == (2 if requested_host_id else 0)
    assert env.host_store.is_online(conv.host_id) is True
    # The session row was re-bound to a fresh runner binding for the
    # new generation.
    rebound = env.conv_store.get_conversation(session_id)
    assert rebound is not None
    assert rebound.host_id == conv.host_id
    assert rebound.runner_id is not None
    assert rebound.runner_id != gen1_runner_id
    # Release the generation-2 tunnel (and the consumed generation-1
    # communicator) only after the assertions.
    second_tunnel = await host_futures[1]
    del first_tunnel, second_tunnel


async def test_resumable_managed_wake_ignores_stale_db_liveness(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused Islo host wakes and signals re-address when online cross-replica."""
    from omnigent.server.routes import sessions as sessions_module

    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    host_store.register_managed_host(
        host_id="40bb7200abc8ed27d5b2fcbfad8e89d2",
        name="managed-stale-live-islo",
        user_id=RESERVED_USER_LOCAL,
        token="tok-stale-live-islo",
        provider="islo",
        sandbox_id="sb-stale-live-islo",
        token_expires_at=9_999_999_999,
    )
    host_store.upsert_on_connect(
        host_id="40bb7200abc8ed27d5b2fcbfad8e89d2",
        name="managed-stale-live-islo",
        user_id=RESERVED_USER_LOCAL,
    )
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id="40bb7200abc8ed27d5b2fcbfad8e89d2",
        workspace="/root/workspace",
    )
    fake = FakeSandboxLauncher(can_resume=True)
    fake.provider = "islo"  # type: ignore[misc]
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://managed-test.example.com",
            launcher_factory=lambda: fake,
            token_ttl_s=3600,
            provider="islo",
        )
    )
    tracker = ManagedLaunchTracker()
    calls: list[str] = []

    def _finish_wake(**kwargs: object) -> None:
        # The wake gets the host's provider, which its seeded launch status names.
        calls.append(f"wake {kwargs['provider']}")
        tracker.begin(conv.id)
        tracker.finish(conv.id)

    monkeypatch.setattr(sessions_module, "_kick_managed_wake", _finish_wake)
    app_state = SimpleNamespace(
        host_store=host_store,
        sandbox_config=config,
        managed_launches=tracker,
        host_registry=SimpleNamespace(get=lambda _host_id: None),
    )

    assert host_store.is_online("40bb7200abc8ed27d5b2fcbfad8e89d2") is True
    with pytest.raises(OmnigentError) as exc:
        await sessions_module._maybe_relaunch_managed_sandbox(
            session_id=conv.id,
            conv=conv,
            app_state=app_state,
            conversation_store=conv_store,
        )
    assert exc.value.code == ErrorCode.WRONG_REPLICA
    assert calls == ["wake islo"]


async def test_resumable_managed_wake_drops_fresh_local_tunnels_when_provider_paused(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused Islo host wakes stale tunnels and signals re-address when online cross-replica."""
    from omnigent.server.routes import sessions as sessions_module

    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    host_store.register_managed_host(
        host_id="055e31f38d07908f171ebad4ff5cbe9c",
        name="managed-stale-tunnel-islo",
        user_id=RESERVED_USER_LOCAL,
        token="tok-stale-tunnel-islo",
        provider="islo",
        sandbox_id="sb-stale-tunnel-islo",
        token_expires_at=9_999_999_999,
    )
    host_store.upsert_on_connect(
        host_id="055e31f38d07908f171ebad4ff5cbe9c",
        name="managed-stale-tunnel-islo",
        user_id=RESERVED_USER_LOCAL,
    )
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id="055e31f38d07908f171ebad4ff5cbe9c",
        workspace="/root/workspace",
    )
    conv_store.set_runner_id(conv.id, "runner_stale_tunnel")
    conv = conv_store.get_conversation(conv.id)
    assert conv is not None
    assert host_store.is_online("055e31f38d07908f171ebad4ff5cbe9c") is True

    fake = FakeSandboxLauncher(can_resume=True)
    fake.provider = "islo"  # type: ignore[misc]
    fake.is_running = lambda _sandbox_id: False  # type: ignore[method-assign]
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://managed-test.example.com",
            launcher_factory=lambda: fake,
            token_ttl_s=3600,
            provider="islo",
        )
    )
    tracker = ManagedLaunchTracker()
    calls: list[str] = []
    host_deregistered: list[str] = []
    runner_deregistered: list[tuple[str, bool]] = []
    fresh_host_conn = SimpleNamespace(last_frame_at=time.time())
    fresh_runner_session = object()
    host_registry_state: dict[str, object | None] = {"conn": fresh_host_conn}

    def _finish_wake(**kwargs: object) -> None:
        del kwargs
        calls.append("wake")
        tracker.begin(conv.id)
        tracker.finish(conv.id)

    def _deregister_host(host_id: str) -> None:
        host_deregistered.append(host_id)
        host_registry_state["conn"] = None

    monkeypatch.setattr(sessions_module, "_kick_managed_wake", _finish_wake)
    app_state = SimpleNamespace(
        host_store=host_store,
        sandbox_config=config,
        managed_launches=tracker,
        host_registry=SimpleNamespace(
            get=lambda _host_id: host_registry_state["conn"],
            deregister=_deregister_host,
        ),
        tunnel_registry=SimpleNamespace(
            get=lambda _runner_id: fresh_runner_session,
            seconds_since_last_frame=lambda _session: 0.0,
            deregister=lambda runner_id, *, retire: runner_deregistered.append(
                (runner_id, retire)
            ),
        ),
    )

    with pytest.raises(OmnigentError) as exc:
        await sessions_module._maybe_wake_stale_resumable_managed_sandbox(
            session_id=conv.id,
            conv=conv,
            app_state=app_state,
            conversation_store=conv_store,
        )
    assert exc.value.code == ErrorCode.WRONG_REPLICA
    assert calls == ["wake"]
    assert host_deregistered == ["055e31f38d07908f171ebad4ff5cbe9c"]
    # A stale tunnel is torn down, not handed off: its turns stay reconcilable here.
    assert runner_deregistered == [("runner_stale_tunnel", False)]


async def test_managed_wake_fails_when_runner_never_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake cannot publish ready if the launched runner tunnel times out."""
    from omnigent.server.routes import sessions as sessions_module

    session_id = "1a9de2e74d453be7cd665c5290b481b9"
    conv = SimpleNamespace(
        id=session_id,
        labels={},
        host_id="cae8b33eab0bd659c87b00dd29946ade",
        workspace="/root/workspace",
        agent_id=None,
        sub_agent_name=None,
        inference_snapshot=None,
    )
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
    stages: list[tuple[str, str | None]] = []

    async def _resume_noop(*_args: object, **_kwargs: object) -> None:
        pass

    async def _launch_runner(*_args: object, **_kwargs: object) -> object:
        return sessions_module._HostLaunchAttempt(runner_id="runner_never_connects")

    class _HostRegistry:
        def get(self, _host_id: str) -> object:
            return object()

    class _TunnelRegistry:
        async def wait_for_runner(self, _runner_id: str, *, timeout_s: float) -> None:
            del timeout_s

    monkeypatch.setattr("omnigent.server.managed_hosts.resume_managed_host", _resume_noop)
    monkeypatch.setattr(sessions_module, "_launch_runner_on_host", _launch_runner)
    monkeypatch.setattr(
        sessions_module,
        "_publish_sandbox_status",
        lambda _sid, stage, error=None, details=None: stages.append((stage, error)),
    )

    await sessions_module._run_managed_wake(
        session_id=session_id,
        conv=conv,  # type: ignore[arg-type]
        sandbox_config=ManagedSandboxDeployment.single(
            ManagedSandboxConfig(
                server_url="https://managed-test.example.com",
                launcher_factory=lambda: FakeSandboxLauncher(),
                token_ttl_s=3600,
            )
        ),
        tracker=tracker,
        conversation_store=SimpleNamespace(get_conversation=lambda _sid: conv),
        host_store=SimpleNamespace(),
        host_registry=_HostRegistry(),  # type: ignore[arg-type]
        tunnel_registry=_TunnelRegistry(),  # type: ignore[arg-type]
    )

    launch = tracker.get(session_id)
    assert launch is not None
    assert launch.settled.is_set()
    assert launch.error == "managed runner did not connect after launch"
    assert stages[-1] == ("failed", "managed runner did not connect after launch")


@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.parametrize("started_sandbox_id", [None, "microvm-real"])
async def test_managed_launch_fails_when_runner_never_connects(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    replaced: bool,
    started_sandbox_id: str | None,
) -> None:
    """Runner failure releases the launched generation, preserving any replacement."""
    from omnigent.server.managed_hosts import launch_managed_host
    from omnigent.server.routes import sessions as sessions_module

    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation()
    fake = FakeSandboxLauncher(
        started_sandbox_id=started_sandbox_id,
        on_host_start=lambda invocation: host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=RESERVED_USER_LOCAL,
        ),
    )
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://managed-test.example.com",
            launcher_factory=lambda: fake,
            token_ttl_s=3600,
        )
    )
    managed = await launch_managed_host(
        config=config, owner=RESERVED_USER_LOCAL, host_store=host_store
    )
    actual_id = started_sandbox_id or "sb-fake-1"
    if replaced:
        host_store.update_sandbox_id(
            managed.host_id,
            expected_sandbox_id=actual_id,
            expected_token=fake.host_starts[0].token,
            sandbox_id="replacement",
        )
    tracker = ManagedLaunchTracker()
    tracker.begin(conv.id)
    stages: list[tuple[str, str | None]] = []

    async def _launch_runner(*_args: object, **_kwargs: object) -> object:
        return sessions_module._HostLaunchAttempt(runner_id="runner_never_connects")

    class _HostRegistry:
        def get(self, _host_id: str) -> object:
            return object()

    class _TunnelRegistry:
        async def wait_for_runner(self, _runner_id: str, *, timeout_s: float) -> None:
            del timeout_s

    monkeypatch.setattr(sessions_module, "_launch_runner_on_host", _launch_runner)
    monkeypatch.setattr(
        sessions_module,
        "_publish_sandbox_status",
        lambda _sid, stage, error=None, details=None: stages.append((stage, error)),
    )

    await sessions_module._bind_and_launch_managed_runner(
        session_id=conv.id,
        managed=managed,
        sandbox_config=config,
        tracker=tracker,
        conversation_store=conversations,
        host_store=host_store,
        host_registry=_HostRegistry(),  # type: ignore[arg-type]
        tunnel_registry=_TunnelRegistry(),  # type: ignore[arg-type]
    )

    launch = tracker.get(conv.id)
    assert launch is not None
    assert launch.settled.is_set()
    assert launch.error == "managed runner did not connect after launch"
    assert stages[-1] == ("failed", "managed runner did not connect after launch")
    remaining = conversations.get_conversation(conv.id)
    assert remaining is not None
    host = host_store.get_host(managed.host_id)
    if replaced:
        assert remaining.host_id == managed.host_id
        assert host is not None and host.sandbox_id == "replacement"
        assert fake.terminated == []
    else:
        assert remaining.host_id is None
        assert remaining.workspace is None
        assert host is None
        assert fake.terminated == [actual_id]


async def test_managed_launch_fails_and_tears_down_when_harness_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that refuses the harness is a fresh sandbox that will never
    serve this session, so it must be torn down the same as the
    delete-during-provisioning branch already is.
    """
    from omnigent.server.routes import sessions as sessions_module

    session_id = "3f3aa440b7274531a889fa4c25f4bb1b"
    host_id = "b4e372ad19634c62966a2c56aab8cfab"
    conv = SimpleNamespace(id=session_id, labels={}, terminal_launch_args=None)
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
    stages: list[tuple[str, str | None]] = []
    terminated: list[object] = []
    sentinel_host = object()

    async def _launch_runner(*_args: object, **_kwargs: object) -> object:
        return sessions_module._HostLaunchAttempt(
            runner_id="runner_refused_harness",
            error_code=sessions_module._HARNESS_NOT_CONFIGURED_ERROR_CODE,
            error="harness not configured on the sandbox host",
        )

    async def _fake_terminate(host: object, *_args: object, **_kwargs: object) -> None:
        terminated.append(host)

    class _HostRegistry:
        def get(self, _host_id: str) -> object:
            return object()

    class _HostStore:
        def get_host(self, requested_id: str) -> object:
            assert requested_id == host_id
            return sentinel_host

    monkeypatch.setattr(sessions_module, "_launch_runner_on_host", _launch_runner)
    monkeypatch.setattr(
        sessions_module,
        "_publish_sandbox_status",
        lambda _sid, stage, error=None, details=None: stages.append((stage, error)),
    )
    monkeypatch.setattr("omnigent.server.managed_hosts.terminate_managed_host", _fake_terminate)

    await sessions_module._bind_and_launch_managed_runner(
        session_id=session_id,
        managed=ManagedHostLaunch(
            host_id=host_id,
            workspace="/root/workspace",
        ),
        sandbox_config=ManagedSandboxDeployment.single(
            ManagedSandboxConfig(
                server_url="https://managed-test.example.com",
                launcher_factory=lambda: FakeSandboxLauncher(),
                token_ttl_s=3600,
            )
        ),
        tracker=tracker,
        conversation_store=SimpleNamespace(
            set_host_id=lambda _sid, _host_id, _workspace: conv,
            clear_host_binding=lambda _sid, **_kwargs: conv,
        ),
        host_store=_HostStore(),  # type: ignore[arg-type]
        host_registry=_HostRegistry(),  # type: ignore[arg-type]
        tunnel_registry=None,
    )

    launch = tracker.get(session_id)
    assert launch is not None
    assert launch.settled.is_set()
    assert launch.error == "harness not configured on the sandbox host"
    assert stages[-1] == ("failed", "harness not configured on the sandbox host")
    assert terminated == [sentinel_host]


async def test_managed_relaunch_failure_leaves_the_host_identity_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed runner connect on a RELAUNCH generation must not tear down
    the host row: unlike a first launch, ``managed.host_id`` here is an
    existing, persistent identity, not something freshly minted for this
    attempt. Deleting it on a slow connect would destroy the user's host
    binding, not just clean up an orphaned Job.
    """
    from omnigent.server.routes import sessions as sessions_module

    session_id = "d3a6e13f0dbf4b6ea4b6e37f4a76f2a9"
    host_id = "9e6cf6f52ebb4a2c9c9a1a5f8e8f5f42"
    conv = SimpleNamespace(id=session_id, labels={}, terminal_launch_args=None)
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
    terminated: list[object] = []

    async def _launch_runner(*_args: object, **_kwargs: object) -> object:
        return sessions_module._HostLaunchAttempt(runner_id="runner_never_connects")

    async def _fake_terminate(host: object, *_args: object, **_kwargs: object) -> None:
        terminated.append(host)

    class _HostRegistry:
        def get(self, _host_id: str) -> object:
            return object()

    class _TunnelRegistry:
        async def wait_for_runner(self, _runner_id: str, *, timeout_s: float) -> None:
            del timeout_s

    class _HostStore:
        def get_host(self, _requested_id: str) -> object:
            # Should never be called: relaunch_host being set must short-circuit
            # before the lookup, not just before the terminate call.
            raise AssertionError("get_host should not be called for a relaunch failure")

    monkeypatch.setattr(sessions_module, "_launch_runner_on_host", _launch_runner)
    monkeypatch.setattr(sessions_module, "_publish_sandbox_status", lambda *_a, **_kw: None)
    monkeypatch.setattr("omnigent.server.managed_hosts.terminate_managed_host", _fake_terminate)

    await sessions_module._bind_and_launch_managed_runner(
        session_id=session_id,
        managed=ManagedHostLaunch(host_id=host_id, workspace="/root/workspace"),
        sandbox_config=ManagedSandboxDeployment.single(
            ManagedSandboxConfig(
                server_url="https://managed-test.example.com",
                launcher_factory=lambda: FakeSandboxLauncher(),
                token_ttl_s=3600,
            )
        ),
        tracker=tracker,
        conversation_store=SimpleNamespace(
            set_host_id=lambda _sid, _host_id, _workspace: conv,
            clear_host_binding=lambda _sid, **_kwargs: conv,
        ),
        host_store=_HostStore(),  # type: ignore[arg-type]
        host_registry=_HostRegistry(),  # type: ignore[arg-type]
        tunnel_registry=_TunnelRegistry(),  # type: ignore[arg-type]
        relaunch_host=object(),  # type: ignore[arg-type]
    )

    launch = tracker.get(session_id)
    assert launch is not None
    assert launch.settled.is_set()
    assert launch.error == "managed runner did not connect after launch"
    assert terminated == []


async def test_cancel_managed_launch_tasks_returns_while_provision_parked(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Shutdown teardown does not hang on an in-flight managed provision.

    With a provision parked on the fake's gate (a slow provider call),
    the default teardown does not wait for the launch to bind: it
    cancels the task and returns promptly rather than waiting the
    provision out.
    """
    import threading

    from omnigent.server.routes.sessions import cancel_managed_launch_tasks

    env = managed_session_env
    gate = threading.Event()
    fake = FakeSandboxLauncher(provision_gate=gate)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-shutdown-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    assert resp.status_code == 201, resp.text

    # The gate would hold the provision 30s; 5s is the generosity bound.
    await asyncio.wait_for(cancel_managed_launch_tasks(), timeout=5.0)

    # Release the gate so the provision worker thread exits cleanly.
    gate.set()


async def test_lifespan_shutdown_drains_for_the_configured_time(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment's ``sandbox.shutdown_drain_s`` is what server shutdown waits."""
    drains: list[float] = []

    async def record_drain(drain_s: float = 0.0) -> None:
        drains.append(drain_s)

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.cancel_managed_launch_tasks", record_drain
    )
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        sandbox_config=parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://managed-test.example.com",
                "modal": {"image": "docker.io/test/omnigent-host:latest"},
                "shutdown_drain_s": 7,
            }
        ),
    )
    async with app.router.lifespan_context(app):
        assert drains == []
    assert drains == [7.0]


async def test_shutdown_waits_for_an_in_flight_launch_to_bind_its_host(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Shutdown lets a mid-provision launch bind its session to the host.

    Once bound, whichever replica the host tunnels to starts the runner
    and delivers the session's saved messages; cancelled before the
    bind, the session would belong to no host and never start.
    """
    import threading

    from omnigent.server.routes.sessions import cancel_managed_launch_tasks

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    loop = asyncio.get_running_loop()
    gate = threading.Event()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it."""
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(provision_gate=gate, on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-shutdown-drain-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    shutdown = asyncio.create_task(cancel_managed_launch_tasks(drain_s=15.0))
    await asyncio.sleep(0.3)
    assert not shutdown.done()
    gate.set()
    await asyncio.wait_for(shutdown, timeout=15.0)

    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.host_id is not None
    assert conv.labels.get(STARTUP_LABEL) != "failed"
    for future in host_futures:
        future.cancel()


@pytest.mark.parametrize("moved", [False, True], ids=["create", "move"])
async def test_shutdown_cancelling_the_bind_write_keeps_the_session_startable(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    moved: bool,
) -> None:
    """
    A drain that ends while the bind is being written does not fail the session.

    The write still lands, so the host's replica must still find the
    session's startup pending; a "failed" startup would never be resumed.
    A moved session keeps its new sandbox binding too, and the move's claim
    on the runner is dropped so that replica can start the runner.
    """
    import threading

    from omnigent.server.routes.sessions import cancel_managed_launch_tasks

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it."""
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    install_fake_modal_launcher(
        monkeypatch, FakeSandboxLauncher(on_host_start=_start_fake_sandbox_host)
    )
    writing = threading.Event()
    release = threading.Event()
    set_host_id = env.conv_store.set_host_id
    moved_session_id = (
        await _session_released_from_laptop(env, agent_name="move-shutdown-bind-race-agent")
        if moved
        else None
    )

    def slow_set_host_id(*args: Any, **kwargs: Any) -> Any:
        writing.set()
        release.wait(timeout=10)
        return set_host_id(*args, **kwargs)

    monkeypatch.setattr(env.conv_store, "set_host_id", slow_set_host_id)

    if moved_session_id is not None:
        session_id = moved_session_id
        resp = await env.client.post(f"/v1/sessions/{session_id}/managed-host", json={})
        assert resp.status_code == 202, resp.text
    else:
        agent = await create_test_agent(env.client, name="managed-shutdown-bind-race-agent")
        resp = await env.client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "host_type": "managed"},
        )
        assert resp.status_code == 201, resp.text
        session_id = resp.json()["id"]
    await asyncio.wait_for(asyncio.to_thread(writing.wait, 10), timeout=15)

    shutdown = asyncio.create_task(cancel_managed_launch_tasks(drain_s=0.2))
    await asyncio.sleep(0.5)
    # Past the drain, shutdown still waits for the bind write to land.
    assert not shutdown.done()
    release.set()
    await asyncio.wait_for(shutdown, timeout=15)

    conv = env.conv_store.get_conversation(session_id)
    assert conv is not None and conv.host_id is not None
    assert conv.labels.get(STARTUP_LABEL) == "pending"
    if moved:
        assert conv.host_id != _LAPTOP_HOST_ID
        assert conv.runner_id is None
    for future in host_futures:
        future.cancel()


async def test_managed_session_deleted_during_provision_terminates_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Deleting a managed session mid-provision tears the sandbox down.

    The delete route's managed cleanup keys off ``conv.host_id``,
    which the background launch has not bound yet — so the background
    task itself must detect the vanished session at the bind step and
    terminate the sandbox it just provisioned (deleting the host row,
    which also revokes the armed launch token).
    """
    import threading

    env = managed_session_env
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    loop = asyncio.get_running_loop()
    gate = threading.Event()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _start_fake_sandbox_host(invocation: HostStartInvocation) -> None:
        """Spawn the fake sandbox host when the launcher 'starts' it."""
        future = asyncio.run_coroutine_threadsafe(
            _fake_sandbox_host(
                env.app, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake = FakeSandboxLauncher(provision_gate=gate, on_host_start=_start_fake_sandbox_host)
    install_fake_modal_launcher(monkeypatch, fake)

    agent = await create_test_agent(env.client, name="managed-delete-race-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    # Delete while the launch is gate-held mid-provision: the session
    # row has no host binding yet, so the route's managed cleanup is a
    # no-op and the background task owns the teardown.
    delete_resp = await env.client.delete(f"/v1/sessions/{session_id}")
    assert delete_resp.status_code == 200, delete_resp.text

    gate.set()
    # The launch completes (sandbox provisioned, fake host registers),
    # then hits the deleted session at the bind step and tears down.
    deadline = loop.time() + 15.0
    while loop.time() < deadline and not fake.terminated:
        await asyncio.sleep(0.05)
    # Terminated exactly the sandbox it provisioned — a regression here
    # leaks a running sandbox (and a live launch token) for a session
    # that no longer exists.
    assert fake.terminated == ["sb-fake-1"]
    # The host row is gone too: the picker shows no ghost host and the
    # token no longer resolves.
    assert env.host_store.list_hosts(RESERVED_USER_LOCAL) == []
    # The fake host coroutines wait for a launch frame that never comes
    # (the session is gone) — cancel them so their receive timeouts
    # don't surface as unretrieved-exception noise after the test.
    for future in host_futures:
        future.cancel()


async def test_delete_reaped_managed_session_removes_durable_host(
    managed_session_env: ManagedSessionEnv,
) -> None:
    """Deleting a reaped session removes its provider-marked durable host row."""
    env = managed_session_env
    host = env.host_store.register_managed_host(
        host_id="8e616af6da4245de923023927b2f36d7",
        name="managed-reaped-delete",
        user_id=RESERVED_USER_LOCAL,
        token="managed-reaped-delete-token",
        provider="modal",
        sandbox_id="sb-reaped-delete",
        token_expires_at=int(time.time()) + 3600,
    )
    assert env.host_store.detach_stale_managed_sandbox(
        host.host_id,
        sandbox_id="sb-reaped-delete",
        expected_updated_at=host.updated_at,
    )
    assert env.host_store.mark_sandbox_terminated(
        host.host_id,
        sandbox_id="sb-reaped-delete",
    )
    agent = await create_test_agent(env.client, name="managed-reaped-delete-agent")
    conversation = env.conv_store.create_conversation(
        agent_id=agent["id"],
        host_id=host.host_id,
        workspace="/tmp/managed-reaped-delete",
    )

    response = await env.client.delete(f"/v1/sessions/{conversation.id}")

    assert response.status_code == 200, response.text
    assert env.conv_store.get_conversation(conversation.id) is None
    assert env.host_store.get_host(host.host_id) is None


@pytest.mark.parametrize(
    ("query", "expected_capture"), [("?capture_logs=true", True), ("", False)]
)
async def test_delete_managed_session_forwards_capture_logs(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected_capture: bool,
) -> None:
    """``DELETE ?capture_logs=true`` asks managed teardown for the sandbox logs."""
    from omnigent.server import managed_hosts

    env = managed_session_env
    host = env.host_store.register_managed_host(
        host_id="3c4d5e6f7a8b49203a15c6d7e8f9a0b1",
        name="managed-capture-delete",
        user_id=RESERVED_USER_LOCAL,
        token="managed-capture-delete-token",
        provider="modal",
        sandbox_id="sb-capture-delete",
        token_expires_at=int(time.time()) + 3600,
    )
    agent = await create_test_agent(env.client, name="managed-capture-delete-agent")
    conversation = env.conv_store.create_conversation(
        agent_id=agent["id"],
        host_id=host.host_id,
        workspace="/tmp/managed-capture-delete",
    )
    teardowns: list[bool] = []

    async def record_teardown(
        *args: object, keep_if_bound: bool = False, capture_logs: bool = False
    ) -> None:
        assert keep_if_bound
        teardowns.append(capture_logs)

    monkeypatch.setattr(managed_hosts, "terminate_managed_host", record_teardown)

    response = await env.client.delete(f"/v1/sessions/{conversation.id}{query}")

    assert response.status_code == 200, response.text
    assert teardowns == [expected_capture]


def _runner_exited_app(db_uri: str, tmp_path: Path) -> tuple[FastAPI, SqlAlchemyConversationStore]:
    """Build the full server app so ``host.runner_exited`` hits ``_on_runner_exited``.

    :param db_uri: SQLite URI.
    :param tmp_path: Scratch dir for artifacts and the agent cache.
    :returns: Tuple of (app, conversation store).
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conv_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
    )
    return app, conv_store


async def _report_runner_exited(
    app: FastAPI, runner_id: str, *, had_connected: bool | None = None
) -> ApplicationCommunicator:
    """Connect a host to ``app`` and send a ``host.runner_exited`` frame.

    :param app: The full server app.
    :param runner_id: Runner the host reports dead.
    :returns: The live tunnel communicator; keep it referenced while asserting.
    """
    from omnigent.host.frames import HostRunnerExitedFrame

    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=5.0)
    assert accepted["type"] == "websocket.accept", f"tunnel refused: {accepted!r}"
    await comm.send_input({"type": "websocket.receive", "text": _make_hello()})
    report = json.loads(
        encode_host_frame(HostRunnerExitedFrame(runner_id=runner_id, error="exited with code 1"))
    )
    if had_connected is not None:
        report["had_connected"] = had_connected
    await comm.send_input({"type": "websocket.receive", "text": json.dumps(report)})
    return comm


def _runner_exited_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return captured ``runner_exited`` debug-event records."""
    return [r for r in caplog.records if getattr(r, "event_name", None) == "runner_exited"]


async def test_runner_exited_logs_one_event_per_bound_session(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A ``host.runner_exited`` report logs one ``runner_exited`` row per session
    bound to the dead runner, each carrying that session's id.

    The frame names only the runner, so without the server-side lookup the
    debug-table row has no ``session_id`` and cannot be joined to a session.
    """
    app, conv_store = _runner_exited_app(db_uri, tmp_path)
    parent = conv_store.create_conversation(agent_id=None, runner_id="runner_crashed")
    child = conv_store.create_conversation(
        agent_id=None,
        runner_id="runner_crashed",
        parent_conversation_id=parent.id,
    )
    conv_store.create_conversation(agent_id=None, runner_id="runner_other")

    caplog.set_level("WARNING", logger="omnigent.server.routes.host_tunnel")
    _comm = await _report_runner_exited(app, "runner_crashed")
    await poll_until_async(lambda: len(_runner_exited_events(caplog)) >= 2)

    events = _runner_exited_events(caplog)
    assert sorted(getattr(r, "session_id", None) for r in events) == sorted([parent.id, child.id])
    for record in events:
        attributes = getattr(record, "attributes", {})
        assert attributes["host_id"] == _HOST_ID
        assert attributes["runner_id"] == "runner_crashed"


async def test_runner_exited_without_bound_sessions_logs_sessionless_event(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A crash of a runner with no bound session still emits one session-less row."""
    app, _conv_store = _runner_exited_app(db_uri, tmp_path)

    caplog.set_level("WARNING", logger="omnigent.server.routes.host_tunnel")
    _comm = await _report_runner_exited(app, "runner_orphan")
    await poll_until_async(lambda: _runner_exited_events(caplog))
    # Let any (incorrect) extra rows from the same frame land before counting.
    await asyncio.sleep(0.05)

    events = _runner_exited_events(caplog)
    assert len(events) == 1
    assert getattr(events[0], "session_id", None) is None
    attributes = getattr(events[0], "attributes", {})
    assert attributes["host_id"] == _HOST_ID
    assert attributes["runner_id"] == "runner_orphan"


async def test_runner_exited_lookup_failure_logs_event_and_keeps_tunnel(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed bound-session lookup still logs the crash and leaves the host online.

    The report is one-way; letting the callback's error escape would tear down
    the host tunnel and take every other runner on that host offline with it.
    """
    app, conv_store = _runner_exited_app(db_uri, tmp_path)

    def _raise(_runner_id: str) -> list[Conversation]:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(conv_store, "list_conversations_by_runner_id", _raise)
    caplog.set_level("WARNING", logger="omnigent.server.routes.host_tunnel")
    _comm = await _report_runner_exited(app, "runner_crashed")

    def _callback_failures() -> list[logging.LogRecord]:
        return [r for r in caplog.records if "on_runner_exited callback failed" in r.getMessage()]

    await poll_until_async(_callback_failures)

    events = _runner_exited_events(caplog)
    assert len(events) == 1
    assert getattr(events[0], "session_id", None) is None
    assert getattr(events[0], "attributes", {})["runner_id"] == "runner_crashed"
    # The tunnel survived the callback error, so the host is still registered.
    assert app.state.host_registry.get(_HOST_ID) is not None


def _disconnect_decisions(
    caplog: pytest.LogCaptureFixture, session_id: str
) -> list[logging.LogRecord]:
    """Return captured ``runner_disconnect_decision`` records for a session."""
    return [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "runner_disconnect_decision"
        and getattr(r, "session_id", None) == session_id
    ]


def _turn_failed_events(
    caplog: pytest.LogCaptureFixture, session_id: str
) -> list[logging.LogRecord]:
    """Return captured ``session_turn_failed`` records for a session."""
    return [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "session_turn_failed"
        and getattr(r, "session_id", None) == session_id
    ]


def _persisted_task_error(
    conv_store: SqlAlchemyConversationStore, session_id: str
) -> dict[str, str] | None:
    """Return the session's persisted failure-cause labels, if any."""
    from omnigent.server.routes import sessions as sessions_module

    refreshed = conv_store.get_conversation(session_id)
    assert refreshed is not None
    return sessions_module._last_task_error_from_labels(refreshed.labels)


@pytest.mark.parametrize("connection_source", ["replica", "host"])
async def test_runner_exited_after_connect_keeps_idle_session_idle(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    connection_source: str,
) -> None:
    """
    A host death after its runner connected does not fail the idle session.

    When a host process dies (e.g. SSH logout), its runners exit with it and
    the host reports ``host.runner_exited`` for each. A session whose runner
    had connected and finished its last turn lost no work: failing it as
    ``failed_before_start`` painted a red crash banner on an idle session.
    The runner's absence is already surfaced through liveness.
    """
    from omnigent.server import session_live_state

    app, conv_store = _runner_exited_app(db_uri, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        agent = await create_test_agent(client, name="runner-idle")
    conv = conv_store.create_conversation(agent_id=agent["id"], runner_id="runner_ran_idle")
    conv_store.set_session_live_status(conv.id, "idle")
    if connection_source == "replica":
        session_live_state.touch_runner_liveness(["runner_ran_idle"])
        # Persist the setup heartbeat before the crash handler snapshots the session.
        await asyncio.wrap_future(session_live_state.submit("drain_test_writes", lambda: None))
    else:
        assert session_live_state.last_liveness_stamp("runner_ran_idle") is None

    caplog.set_level("WARNING", logger="omnigent")
    _comm = await _report_runner_exited(
        app, "runner_ran_idle", had_connected=True if connection_source == "host" else None
    )
    async with asyncio.timeout(5.0):
        while not _disconnect_decisions(caplog, conv.id):
            await asyncio.sleep(0.01)
    # Let any (incorrect) failure edge from the same report land before asserting.
    await asyncio.sleep(0.05)

    decision = _disconnect_decisions(caplog, conv.id)[0]
    attributes = getattr(decision, "attributes", {})
    assert attributes["decision"] == "idle_no_failure"
    assert attributes["fail_idle_top_level"] is False
    assert not _turn_failed_events(caplog, conv.id)
    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.live_status == "idle"
    assert _persisted_task_error(conv_store, conv.id) is None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/v1/sessions/{conv.id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "idle"
    assert response.json()["last_task_error"] is None


@pytest.mark.parametrize("had_connected", [None, False])
async def test_runner_exited_before_connect_fails_idle_session(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    had_connected: bool | None,
) -> None:
    """
    A runner that died before connecting still fails its idle session.

    That session never reached ``running`` and would otherwise spin on
    "starting" until a timeout; the crash report is its only failure signal.
    """
    app, conv_store = _runner_exited_app(db_uri, tmp_path)
    conv = conv_store.create_conversation(agent_id=None, runner_id="runner_never_connected")
    conv_store.set_session_live_status(conv.id, "idle")

    caplog.set_level("WARNING", logger="omnigent")
    _comm = await _report_runner_exited(app, "runner_never_connected", had_connected=had_connected)
    async with asyncio.timeout(5.0):
        while _persisted_task_error(conv_store, conv.id) is None:
            await asyncio.sleep(0.01)

    error = _persisted_task_error(conv_store, conv.id)
    assert error is not None
    assert error["code"] == "runner_failed_to_start"
    decision = _disconnect_decisions(caplog, conv.id)[0]
    attributes = getattr(decision, "attributes", {})
    assert attributes["decision"] == "failed_before_start"
    assert attributes["fail_idle_top_level"] is True
    failed = _turn_failed_events(caplog, conv.id)
    assert failed
    assert getattr(failed[0], "attributes", {})["code"] == "runner_failed_to_start"


@pytest.mark.parametrize("connection_source", ["replica", "host"])
async def test_runner_exited_after_connect_fails_mid_turn_session(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    connection_source: str,
) -> None:
    """
    A connected runner's death still fails a session that was mid-turn.

    The host dying interrupts in-flight work, so the turn fails with the
    daemon-reported crash cause.
    """
    from omnigent.server import session_live_state

    app, conv_store = _runner_exited_app(db_uri, tmp_path)
    conv = conv_store.create_conversation(agent_id=None, runner_id="runner_ran_mid_turn")
    conv_store.set_session_live_status(conv.id, "running")
    if connection_source == "replica":
        session_live_state.touch_runner_liveness(["runner_ran_mid_turn"])
        # A heartbeat landing during the crash sweep looks like a runner reconnect.
        await asyncio.wrap_future(session_live_state.submit("drain_test_writes", lambda: None))
    else:
        assert session_live_state.last_liveness_stamp("runner_ran_mid_turn") is None

    caplog.set_level("WARNING", logger="omnigent")
    _comm = await _report_runner_exited(
        app, "runner_ran_mid_turn", had_connected=True if connection_source == "host" else None
    )
    async with asyncio.timeout(5.0):
        while _persisted_task_error(conv_store, conv.id) is None:
            await asyncio.sleep(0.01)

    error = _persisted_task_error(conv_store, conv.id)
    assert error is not None
    assert error["code"] == "runner_process_crash"
    decision = _disconnect_decisions(caplog, conv.id)[0]
    attributes = getattr(decision, "attributes", {})
    assert attributes["decision"] == "failed_mid_turn"
    assert attributes["fail_idle_top_level"] is False


_CONCRETE_FAILURE = "Claude Code could not continue: the project settings file is not valid JSON."


async def _answer_host_launch(comm: ApplicationCommunicator) -> None:
    """Acknowledge one launch (and any workspace stat) without spawning a runner."""
    from omnigent.host.frames import HostStatFrame, HostStatResultFrame

    for _ in range(40):
        output = await comm.receive_output(timeout=5.0)
        if output["type"] != "websocket.send":
            continue
        frame = decode_host_frame(output["text"])
        if isinstance(frame, HostStatFrame):
            reply = HostStatResultFrame(
                request_id=frame.request_id,
                status="ok",
                exists=True,
                type="directory",
                canonical_path=frame.path,
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            reply = HostLaunchRunnerResultFrame(
                request_id=frame.request_id, status="launched", runner_id="runner_token_test"
            )
        else:
            continue
        await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(reply)})
        if isinstance(reply, HostLaunchRunnerResultFrame):
            return
    raise AssertionError("host launch was not requested within 40 frames")


async def _failed_native_session_on_never_connected_runner(
    app: FastAPI, client: AsyncClient
) -> tuple[str, str, dict[str, str], ApplicationCommunicator]:
    """Create a native session, bind it to a host-launched runner that never
    tunnels, then deliver the forwarder's ``external_session_status: failed``.

    :returns: Session id, bound runner id, the persisted ``last_task_error``, and
        the scripted host's tunnel (keep it referenced while asserting).
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )

    agent = await create_test_agent(client, name="claude-native-ui")
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "labels": {
                UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
            },
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    host = await _connect_host(app, app.state.host_registry)
    responder = asyncio.create_task(_answer_host_launch(host))
    launched = await client.post(
        f"/v1/hosts/{_HOST_ID}/runners", json={"session_id": session_id, "workspace": "/tmp"}
    )
    assert launched.status_code == 200, launched.text
    await responder

    failed = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "failed", "output": _CONCRETE_FAILURE},
        },
    )
    assert failed.status_code in (200, 202), failed.text
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    runner_id = snapshot["runner_id"]
    assert isinstance(runner_id, str) and runner_id.startswith("runner_token_")
    assert snapshot["host_id"] == _HOST_ID
    assert snapshot["status"] == "failed"
    assert snapshot["last_task_error"]["message"] == _CONCRETE_FAILURE
    return session_id, runner_id, snapshot["last_task_error"], host


async def test_runner_exited_keeps_existing_failure_cause(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A runner exit report does not replace a session's persisted failure cause.

    A native session that already failed with a concrete reason keeps that
    reason, and gets no second failed turn, when its never-connected runner
    is later reported dead.
    """
    from tests.server.helpers import start_session_stream_collector

    app, conv_store = _runner_exited_app(db_uri, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        (
            session_id,
            runner_id,
            original_error,
            _host,
        ) = await _failed_native_session_on_never_connected_runner(app, client)
        assert _persisted_task_error(conv_store, session_id) == original_error

        collector = await start_session_stream_collector(session_id)
        caplog.set_level("WARNING", logger="omnigent")
        _comm = await _report_runner_exited(app, runner_id)
        async with asyncio.timeout(5.0):
            while not _disconnect_decisions(caplog, session_id):
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        await collector.stop()
        edges = []
        while not collector.queue.empty():
            edges.append(collector.queue.get_nowait())
        after = (await client.get(f"/v1/sessions/{session_id}")).json()

    sweep_failures = [
        r
        for r in _turn_failed_events(caplog, session_id)
        if getattr(r, "attributes", {}).get("origin") == "runner_offline_sweep"
    ]
    assert not sweep_failures
    assert not [
        e for e in edges if e.get("type") == "session.status" and e.get("status") == "failed"
    ]
    assert _persisted_task_error(conv_store, session_id) == original_error
    assert after["status"] == "failed"
    assert after["last_task_error"] == original_error


async def test_session_response_keeps_existing_failure_cause_while_exit_report_is_held(
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The session response does not swap a persisted failure cause for a cached exit report.

    With connection evidence the exit handler leaves the failed session alone,
    so the snapshot must keep reporting the cause the labels still hold.
    """
    app, conv_store = _runner_exited_app(db_uri, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        (
            session_id,
            runner_id,
            original_error,
            _host,
        ) = await _failed_native_session_on_never_connected_runner(app, client)

        caplog.set_level("WARNING", logger="omnigent")
        _comm = await _report_runner_exited(app, runner_id, had_connected=True)
        async with asyncio.timeout(5.0):
            while not _disconnect_decisions(caplog, session_id):
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        after = (await client.get(f"/v1/sessions/{session_id}")).json()

    decision = getattr(_disconnect_decisions(caplog, session_id)[0], "attributes", {})
    assert decision["decision"] == "idle_no_failure"
    assert _persisted_task_error(conv_store, session_id) == original_error
    assert after["status"] == "failed"
    assert after["last_task_error"] == original_error
