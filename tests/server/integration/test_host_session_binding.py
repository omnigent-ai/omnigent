"""Integration tests for session creation with host_id and reconnect reconciliation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.entities import Conversation
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostRegistry
from omnigent.server.managed_hosts import (
    ManagedHostLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    parse_sandbox_config,
)
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
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

_HOST_ID = "3f866cafac81246fb60ae6ceb1a738da"


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
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """App with host tunnel + REST routes for binding tests.

    :param db_uri: SQLite URI.
    :returns: Tuple of (app, registry, host_store, conv_store).
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
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


async def test_launch_runner_writes_host_id_and_runner_id(
    binding_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify that POST /hosts/{id}/runners writes both runner_id and
    host_id to the session row.

    If either is missing after the call, the binding flow in the
    launch endpoint is incomplete and reconnect reconciliation
    won't be able to find the session on host reconnect.
    """
    app, registry, _hs, conv_store = binding_app
    comm = await _connect_host(app, registry)
    conv = conv_store.create_conversation(agent_id=None)

    async def _respond_launched() -> None:
        """Respond to the launch frame from the host."""
        for _ in range(20):
            output = await comm.receive_output(timeout=2.0)
            if output["type"] != "websocket.send":
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostLaunchRunnerFrame):
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
        if conv.runner_id is not None:
            return conv
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"background managed launch never bound session {session_id} within {timeout_s}s"
    )


async def test_managed_session_create_end_to_end(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /v1/sessions`` with ``host_type="managed"`` returns
    immediately and provisions in the background: a sandbox is
    provisioned, the sandbox host registers over the REAL tunnel with
    the minted token, and the session ends up bound to that host with
    a runner launched on it — everything but the sandbox is production
    code.
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

    agent = await create_test_agent(env.client, name="managed-host-agent")
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
    assert resp.status_code == 201, resp.text
    # The non-blocking contract: the create response carries no host
    # binding — provisioning is still running in the background.
    assert resp.json()["host_id"] is None
    session_id = resp.json()["id"]

    # The background launch binds the session row: sandbox host,
    # sandbox workspace, and a token-bound runner.
    conv = await _wait_for_managed_binding(env, session_id)
    # Keep the fake hosts' tunnels alive (referenced) for the rest of
    # the test — the host store flips them offline if they drop.
    tunnels = [await future for future in host_futures]
    assert len(tunnels) == 1
    assert conv.host_id is not None
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

    # Deleting the managed session tears the sandbox down: the
    # provider terminate fires and the host row is deleted — which
    # both revokes the launch token and removes the dead host from
    # the picker (no offline ghost lingering after the session).
    delete_resp = await env.client.delete(f"/v1/sessions/{session_id}")
    assert delete_resp.status_code == 200, delete_resp.text
    assert fake.terminated == ["sb-fake-1"]
    assert env.host_store.get_host(conv.host_id) is None
    assert env.host_store.list_hosts(RESERVED_USER_LOCAL) == []
    # The tunnels list holds the fake hosts open through the delete;
    # release them only now.
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
    assert "takes a git repository URL" in detail[0]["msg"]


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
    provision rendezvouses on the launch instead of failing fast.

    The fake launcher holds ``provision`` on a gate, pinning the
    background launch mid-provision deterministically. While held:

    - The create POST has ALREADY returned (with the old synchronous
      create, the POST itself would block on the gate and this test
      would hang at the first await).
    - A message POST does not resolve — pre-fix it returned 503
      "No runner bound for session" immediately, desyncing the Web
      UI's auto-sent first prompt from the provisioning sandbox.

    Releasing the gate lets the launch proceed into a host-start
    failure, which must surface the recorded reason on the waiting
    message POST (and on later ones), and tear the sandbox down.
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
    assert snapshot.json()["sandbox_status"] == {"stage": "provisioning", "error": None}

    message_task = asyncio.create_task(
        env.client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello sandbox"}],
                },
            },
        )
    )
    # The message parks on the launch rendezvous while provision is
    # held. Pre-fix this resolved immediately with 503 "No runner
    # bound for session" — well inside the 0.3s window, so a regression
    # turns this wait into a completed task and fails the assert.
    done, _ = await asyncio.wait({message_task}, timeout=0.3)
    assert not done, (
        f"message POST resolved during provisioning instead of waiting: "
        f"{message_task.result().status_code} {message_task.result().text}"
    )

    gate.set()
    message_resp = await asyncio.wait_for(message_task, timeout=15.0)
    # The launch failed at host start; the parked message reports the
    # recorded launch failure, not a generic "no runner bound".
    assert message_resp.status_code == 503, message_resp.text
    assert "managed sandbox failed to launch" in message_resp.text
    assert "host startup failed" in message_resp.text

    # A later message hits the retained failure record immediately.
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
    assert snapshot.json()["sandbox_status"] == {"stage": "provisioning", "error": None}

    # Tail live progress at the layer the SSE endpoint reads it from.
    # (The endpoint itself never terminates under the in-process ASGI
    # transport — see the stream test in test_sessions_permissions.)
    stages: list[str] = []
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

    # A settled launch clears the snapshot field — from here the
    # session looks like any host-bound session and a reloading
    # client sees no provisioning indicator.
    snapshot_after = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot_after.status_code == 200, snapshot_after.text
    assert snapshot_after.json()["sandbox_status"] is None
    del tunnels


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


async def test_message_relaunches_dead_managed_sandbox(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
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
    resp = await env.client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_type": "managed"},
    )
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
    """A paused Islo host wakes even while its DB liveness row is still fresh."""
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
    config = ManagedSandboxConfig(
        server_url="https://managed-test.example.com",
        launcher_factory=lambda: fake,
        token_ttl_s=3600,
        provider="islo",
    )
    tracker = ManagedLaunchTracker()
    calls: list[str] = []

    def _finish_wake(**kwargs: object) -> None:
        del kwargs
        calls.append("wake")
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
    assert (
        await sessions_module._maybe_relaunch_managed_sandbox(
            session_id=conv.id,
            conv=conv,
            app_state=app_state,
            conversation_store=conv_store,
        )
        is True
    )
    assert calls == ["wake"]


async def test_resumable_managed_wake_drops_fresh_local_tunnels_when_provider_paused(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused Islo host can wake before local tunnel liveness notices."""
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
    config = ManagedSandboxConfig(
        server_url="https://managed-test.example.com",
        launcher_factory=lambda: fake,
        token_ttl_s=3600,
        provider="islo",
    )
    tracker = ManagedLaunchTracker()
    calls: list[str] = []
    host_deregistered: list[str] = []
    runner_deregistered: list[str] = []
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
            deregister=lambda runner_id: runner_deregistered.append(runner_id),
        ),
    )

    assert (
        await sessions_module._maybe_wake_stale_resumable_managed_sandbox(
            session_id=conv.id,
            conv=conv,
            app_state=app_state,
            conversation_store=conv_store,
        )
        is True
    )
    assert calls == ["wake"]
    assert host_deregistered == ["055e31f38d07908f171ebad4ff5cbe9c"]
    assert runner_deregistered == ["runner_stale_tunnel"]


async def test_managed_wake_fails_when_runner_never_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake cannot publish ready if the launched runner tunnel times out."""
    from omnigent.server.routes import sessions as sessions_module

    session_id = "1a9de2e74d453be7cd665c5290b481b9"
    conv = SimpleNamespace(
        id=session_id,
        host_id="cae8b33eab0bd659c87b00dd29946ade",
        workspace="/root/workspace",
        agent_id=None,
        sub_agent_name=None,
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
        lambda _sid, stage, error=None: stages.append((stage, error)),
    )

    await sessions_module._run_managed_wake(
        session_id=session_id,
        conv=conv,  # type: ignore[arg-type]
        sandbox_config=ManagedSandboxConfig(
            server_url="https://managed-test.example.com",
            launcher_factory=lambda: FakeSandboxLauncher(),
            token_ttl_s=3600,
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


async def test_managed_launch_fails_when_runner_never_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial managed launch settlement also depends on the runner tunnel."""
    from omnigent.server.routes import sessions as sessions_module

    session_id = "1a41e11887aca4570e42c0be40f24833"
    conv = SimpleNamespace(id=session_id)
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
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
        lambda _sid, stage, error=None: stages.append((stage, error)),
    )

    await sessions_module._bind_and_launch_managed_runner(
        session_id=session_id,
        managed=ManagedHostLaunch(
            host_id="3c8cdcdb61903904849174c864620a5c",
            workspace="/root/workspace",
        ),
        sandbox_config=ManagedSandboxConfig(
            server_url="https://managed-test.example.com",
            launcher_factory=lambda: FakeSandboxLauncher(),
            token_ttl_s=3600,
        ),
        tracker=tracker,
        conversation_store=SimpleNamespace(
            set_host_id=lambda _sid, _host_id, _workspace: conv,
        ),
        host_store=SimpleNamespace(),
        host_registry=_HostRegistry(),  # type: ignore[arg-type]
        tunnel_registry=_TunnelRegistry(),  # type: ignore[arg-type]
    )

    launch = tracker.get(session_id)
    assert launch is not None
    assert launch.settled.is_set()
    assert launch.error == "managed runner did not connect after launch"
    assert stages[-1] == ("failed", "managed runner did not connect after launch")


async def test_cancel_managed_launch_tasks_returns_while_provision_parked(
    managed_session_env: ManagedSessionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Shutdown teardown does not hang on an in-flight managed provision.

    The lifespan teardown calls ``cancel_managed_launch_tasks`` to
    stop background launches; with a provision parked on the fake's
    gate (a slow provider call), the cancel must settle the task and
    return promptly rather than waiting the provision out — a rolling
    deploy must not block on Modal. Without the cancellation hook,
    this test fails at the ``wait_for`` (nothing settles the task).
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

    # The launch is gate-held mid-provision. The teardown hook must
    # return well inside the provision's duration (the gate would hold
    # it 30s) — 5s is the generosity bound, not an expectation.
    await asyncio.wait_for(cancel_managed_launch_tasks(), timeout=5.0)

    # Release the gate so the provision worker thread exits cleanly.
    gate.set()


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
