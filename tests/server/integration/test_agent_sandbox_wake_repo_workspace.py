"""Regression: waking ``agent_sandbox`` must rebuild recorded repo workspaces.

User journey:

1. Create a managed ``agent_sandbox`` session with one or two repositories.
   The sandbox clones them beneath ``/root/workspace`` and binds there.
2. The sandbox suspends: its Pod is deleted (the ``emptyDir`` HOME is lost) but
   the Sandbox CR is retained (``shutdownPolicy: Retain``), so the host goes
   offline.
3. Send another message to the same session, which wakes the host in place —
   the messages route spawns the background wake (``_kick_managed_wake`` →
   ``_run_managed_wake``), which resumes the sandbox and launches a runner in
   the session's recorded workspace.
4. On a broken wake recorded repository directories are absent from the fresh
   ephemeral HOME, so the session is incomplete.

This drives the REAL ``_run_managed_wake`` orchestration (the exact coroutine a
message POST spawns for a dormant resumable host) against the production
``create_app`` and host tunnel; only the sandbox itself is faked. The fake
models the agent-sandbox provider faithfully — resume wipes the ephemeral HOME,
``start_host`` recreates only what it is told to — and mirrors the real host's
workspace check in ``omnigent/host/connect.py`` (refuse with
``workspace_missing`` when the workspace dir is absent). Whether each repo is
reconstructed on wake therefore depends entirely on what the real wake path
passes into ``start_host``; the fake presupposes nothing.

The test asserts that the wake reconstructs every recorded repository and the
runner launch succeeds for both provider capability shapes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import ClassVar

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.entities import Conversation
from omnigent.host.frames import (
    WORKSPACE_MISSING_ERROR_CODE,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities
from omnigent.runner.identity import token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.host_registry import HostRegistry
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    RepoWorkspace,
)
from omnigent.server.routes import sessions as sessions_module
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
)

pytestmark = pytest.mark.asyncio

_SINGLE_REPO_WORKSPACES = ["https://github.com/org/myrepo.git#main"]
_MULTI_REPO_WORKSPACES = [
    "https://github.com/org/api.git#main",
    "https://github.com/org/web.git#release",
]
_WORKSPACE_DIR = "/root/workspace"


class _AgentSandboxFake(FakeSandboxLauncher):
    """A resumable ``agent_sandbox`` launcher whose HOME is EPHEMERAL.

    Models the Kubernetes agent-sandbox provider: the sandbox resumes in place
    (``can_resume``) with its Sandbox CR retained, but HOME is an ``emptyDir``
    volume that comes back EMPTY when the Pod is recreated on resume.

    ``live_dirs`` tracks which directories currently exist inside the sandbox:

    * ``resume`` wipes it (fresh empty ``emptyDir`` HOME after the Pod recreate);
    * ``start_host`` recreates the ``<HOME>/workspace`` root it always ``mkdir``s
      and, for every handed repo, the ``<workspace>/<repo_name>`` clone
      directory (mirrors ``ExecModelHostLauncher.start_host`` /
      ``materialize_workspace``).

    The fake never presupposes the bug: which repos are reconstructed on wake
    depends entirely on which repos the real wake path passes into
    ``start_host``.
    """

    provider: ClassVar[str] = "agent_sandbox"

    @property
    def capabilities(self) -> SandboxCapabilities:
        return replace(super().capabilities, multi_repo=self._multi_repo)

    def __init__(self, *, multi_repo: bool) -> None:
        super().__init__(can_resume=True)
        self._multi_repo = multi_repo
        self.live_dirs: set[str] = set()
        self.resume_count = 0
        # Every launch result the fake host answered, in arrival order.
        self.launch_results: list[HostLaunchRunnerResultFrame] = []

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage=None,
    ) -> str:
        """Record the dirs the real bootstrap creates, then run it.

        Recording BEFORE delegating matters: the base ``start_host`` fires the
        ``on_host_start`` callback (which connects the fake host tunnel) as its
        final step, so ``live_dirs`` must already reflect the filesystem by the
        time a launch frame can arrive.
        """
        workspace = f"{self._home}/workspace"
        # `mkdir -p <workspace>` always runs in ExecModelHostLauncher.start_host.
        self.live_dirs.add(workspace)
        for repo in repos:
            self.live_dirs.add(f"{workspace}/{repo.repo_name}")
        if on_stage is not None:
            on_stage("starting")
        if self._on_host_start is not None:
            self._on_host_start(
                HostStartInvocation(
                    host_id=host_id,
                    host_name=host_name,
                    token=token,
                    command="",
                )
            )
        return f"{workspace}/{repos[0].repo_name}" if len(repos) == 1 else workspace

    def resume(self, sandbox_id: str) -> None:
        """Resume in place — the Pod recreates with a FRESH, EMPTY HOME."""
        self.resume_count += 1
        self.live_dirs.clear()
        super().resume(sandbox_id)


def _websocket_scope(path: str) -> dict[str, object]:
    """Build a minimal ASGI WebSocket scope for the host tunnel."""
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


def _make_hello(name: str) -> str:
    """Encode a HostHelloFrame the fake sandbox host registers with."""
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
            runners=[],
        )
    )


async def _serve_agent_sandbox_host(
    app: FastAPI,
    fake: _AgentSandboxFake,
    host_id: str,
    host_name: str,
    token: str,
) -> ApplicationCommunicator:
    """Act as the host process inside the (fake) sandbox.

    Connects to the app's REAL host tunnel with only the launch token, sends
    hello, then answers the first launch frame the way the real host in
    ``omnigent/host/connect.py`` does: ``launched`` when the requested workspace
    directory exists in the sandbox, else refuse with ``workspace_missing``.
    The answered result is recorded on ``fake.launch_results``.

    :returns: The live tunnel communicator; the caller must keep it referenced
        so the ASGI task (and the online host row) stay alive.
    """
    scope = _websocket_scope(f"/v1/hosts/{host_id}/tunnel")
    scope["headers"] = [(b"x-omnigent-host-token", token.encode("ascii"))]
    comm = ApplicationCommunicator(app, scope)
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=5.0)
    assert accepted["type"] == "websocket.accept", f"tunnel refused: {accepted!r}"
    await comm.send_input(
        {"type": "websocket.receive", "text": _make_hello(name=host_name)},
    )
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
            # Mirror the real host: a launch into a missing workspace is
            # refused with workspace_missing.
            if frame.workspace in fake.live_dirs:
                result = HostLaunchRunnerResultFrame(
                    request_id=frame.request_id,
                    status="launched",
                    runner_id=token_bound_runner_id(frame.binding_token),
                )
            else:
                result = HostLaunchRunnerResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error=f"workspace path does not exist: {frame.workspace}",
                    error_code=WORKSPACE_MISSING_ERROR_CODE,
                )
            fake.launch_results.append(result)
            await comm.send_input(
                {"type": "websocket.receive", "text": encode_host_frame(result)},
            )
            return comm
    raise AssertionError("fake agent_sandbox host never received a launch frame")


async def _wait_for_binding(
    conv_store: SqlAlchemyConversationStore,
    session_id: str,
    *,
    timeout_s: float = 15.0,
) -> Conversation:
    """Poll the session row until the background managed launch binds it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        conv = conv_store.get_conversation(session_id)
        assert conv is not None, "session row vanished while awaiting managed binding"
        if conv.runner_id is not None:
            return conv
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"background managed launch never bound session {session_id} within {timeout_s}s"
    )


@pytest.mark.parametrize(
    ("repo_workspaces", "multi_repo", "expected_workspace", "expected_clone_dirs"),
    [
        pytest.param(
            _SINGLE_REPO_WORKSPACES,
            False,
            f"{_WORKSPACE_DIR}/myrepo",
            {f"{_WORKSPACE_DIR}/myrepo"},
            id="single-repo",
        ),
        pytest.param(
            _MULTI_REPO_WORKSPACES,
            True,
            _WORKSPACE_DIR,
            {f"{_WORKSPACE_DIR}/api", f"{_WORKSPACE_DIR}/web"},
            id="multiple-repos",
        ),
    ],
)
async def test_agent_sandbox_wake_reconstructs_repo_workspaces(
    runtime_init: None,
    db_uri: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    repo_workspaces: list[str],
    multi_repo: bool,
    expected_workspace: str,
    expected_clone_dirs: set[str],
) -> None:
    """Waking a resumable agent_sandbox host must rebuild its repo workspaces.

    Create a managed agent_sandbox session with recorded repositories, suspend
    it (ephemeral HOME lost), then drive the real send-message wake
    (``_run_managed_wake``). The wake must reconstruct the recorded repository
    directories so its runner launch succeeds and each checkout is present.
    """
    # A healthy fake host registers well under a second; shrink the online-poll
    # budget so a wake/registration regression fails in seconds.
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 10)
    # Keep the runner-connect wait short (it is stubbed to succeed below, so this
    # only bounds any residual waiting).
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S", 0.2
    )

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)

    fake = _AgentSandboxFake(multi_repo=multi_repo)
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://managed-test.example.com",
            launcher_factory=lambda: fake,
            token_ttl_s=3600,
            provider="agent_sandbox",
        )
    )
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
        sandbox_config=config,
    )

    loop = asyncio.get_running_loop()
    host_futures: list[asyncio.Future[ApplicationCommunicator]] = []

    def _spawn_fake_host(invocation: HostStartInvocation) -> None:
        """Spawn a fresh fake sandbox host when the launcher 'starts' one.

        Runs on the launcher's worker thread (start_host executes under
        ``asyncio.to_thread``), so the coroutine is handed back to the app's
        event loop.
        """
        future = asyncio.run_coroutine_threadsafe(
            _serve_agent_sandbox_host(
                app, fake, invocation.host_id, invocation.host_name, invocation.token
            ),
            loop,
        )
        host_futures.append(asyncio.wrap_future(future, loop=loop))

    fake._on_host_start = _spawn_fake_host

    host_registry: HostRegistry = app.state.host_registry
    # Stub the runner-connect rendezvous to succeed (no real runner process here).
    monkeypatch.setattr(
        app.state.tunnel_registry,
        "wait_for_runner",
        lambda _runner_id, *, timeout_s: asyncio.sleep(0, result=object()),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        workspace_request = (
            {"workspaces": repo_workspaces} if multi_repo else {"workspace": repo_workspaces[0]}
        )
        # Create a managed agent_sandbox session with recorded repositories.
        agent = await create_test_agent(client, name="agent-sandbox-wake-agent")
        resp = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "host_type": "managed",
                **workspace_request,
            },
        )
        assert resp.status_code == 201, resp.text
        session_id = resp.json()["id"]

        conv = await _wait_for_binding(conv_store, session_id)
        gen1_tunnel = await host_futures[0]

        # Repositories are cloned and the session binds to the expected workspace.
        assert conv.host_id is not None
        assert conv.runner_id is not None
        assert conv.workspace == expected_workspace
        assert fake.live_dirs >= expected_clone_dirs, (
            f"initial launch did not clone each repo; live dirs={sorted(fake.live_dirs)}"
        )
        host_id = conv.host_id

        # Settle creation so suspension takes the offline/wake branch.
        tracker = app.state.managed_launches
        deadline = loop.time() + 10.0
        while loop.time() < deadline and tracker.get(session_id) is not None:
            await asyncio.sleep(0.05)
        assert tracker.get(session_id) is None, "create launch never settled"

        # 2. Suspend: the sandbox's Pod is deleted (the CR is retained), so the
        # host goes offline. A clean tunnel close is the state a suspend leaves.
        await gen1_tunnel.send_input({"type": "websocket.disconnect", "code": 1000})
        deadline = loop.time() + 10.0
        while loop.time() < deadline and (
            host_registry.get(host_id) is not None or host_store.is_online(host_id)
        ):
            await asyncio.sleep(0.05)
        assert host_registry.get(host_id) is None
        assert host_store.is_online(host_id) is False

        # Drive the message route's wake coroutine against a fresh ephemeral HOME.
        refreshed = conv_store.get_conversation(session_id)
        assert refreshed is not None
        assert refreshed.workspace == expected_workspace
        tracker.begin(session_id)
        await sessions_module._run_managed_wake(
            session_id=session_id,
            conv=refreshed,
            sandbox_config=config,
            tracker=tracker,
            conversation_store=conv_store,
            host_store=host_store,
            host_registry=host_registry,
            tunnel_registry=app.state.tunnel_registry,
            agent_store=getattr(app.state, "agent_store", None),
            agent_id=refreshed.agent_id,
        )
        gen2_tunnel = await host_futures[1]

        assert fake.resume_count == 1, "wake did not resume the sandbox in place"
        assert len(fake.provisioned_names) == 1, (
            "wake should resume the SAME sandbox in place, not provision a fresh "
            f"generation; got provisions {fake.provisioned_names}"
        )
        assert host_store.is_online(host_id) is True, "host never came back online after wake"
        assert host_registry.get(host_id) is not None, "no live host tunnel after wake"
        assert tracker.get(session_id) is None, "wake never settled the launch tracker"

        # The wake must launch a runner in the recorded workspace.
        assert len(fake.launch_results) == 2, (
            f"expected the create launch and the wake launch; got {fake.launch_results!r}"
        )
        wake_launch = fake.launch_results[-1]

        # Missing restoration is surfaced as workspace_missing.
        assert wake_launch.error_code != WORKSPACE_MISSING_ERROR_CODE, (
            "waking a resumable agent_sandbox host with an ephemeral HOME rejected "
            "the runner launch with workspace_missing: the wake never re-cloned the "
            f"recorded repository workspace {refreshed.workspace!r} into the fresh "
            f"emptyDir HOME. Got {wake_launch!r}."
        )
        assert wake_launch.status == "launched", (
            f"runner launch refused after wake: {wake_launch!r}"
        )
        assert fake.live_dirs >= expected_clone_dirs, (
            "woken sandbox did not re-clone each recorded repository; "
            f"live dirs={sorted(fake.live_dirs)}"
        )

        # Release the tunnels only after the assertions.
        del gen1_tunnel, gen2_tunnel
