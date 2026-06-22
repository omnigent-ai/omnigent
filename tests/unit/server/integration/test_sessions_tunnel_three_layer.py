"""
Tunnel three-layer integration test: Omnigent → WS tunnel → runner → harness.

Production-shaped variant of ``test_sessions_three_layer.py``. The
prior test wired the AP's ``_runner_client`` directly to the runner
app via ``httpx.ASGITransport``, which short-circuits the WebSocket
tunnel that ships in production. This test instead drives the real
``/v1/runners/{runner_id}/tunnel`` route with an
``asgiref.testing.ApplicationCommunicator`` and pumps
``RequestFrame`` traffic through ``dispatch_via_asgi`` into a real
``create_runner_app``. The AP's ``RunnerRouter`` ends up serving a
``WSTunnelTransport``-backed client (the same code path the real
server uses), so every native-sessions HTTP hop crosses the live
tunnel registry.

Catches bugs that the ASGI-only three-layer test misses: frame
encode/decode regressions, tunnel registry leak/registration races,
``WSTunnelTransport`` error mapping, and any AP-side code that
reaches for ``app.state.tunnel_registry`` / ``get_runner_router()``.

Layers:
    Omnigent server ──HTTP──> RunnerRouter ──WSTunnelTransport──>
        TunnelRegistry ──tunnel WS route──> ApplicationCommunicator ──>
        forwarder task ──> create_runner_app ──> FakeProcessManager ──>
        EchoHarness ASGI app

The TODO at the end of the module flags ``FakeProcessManager`` and
``_build_harness_agent_bundle`` as candidates to factor out of
``test_sessions_three_layer.py`` once a second consumer (this file)
lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tarfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import yaml
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from omnigent_client._events import ResponseCompleted, ResponseFailed
from omnigent_client._files import FilesNamespace
from omnigent_client._sessions import SessionsNamespace

from omnigent.repl._repl import _SessionsChatReplAdapter
from omnigent.runner.app import create_runner_app
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    RequestFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.serve import dispatch_via_asgi
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.runtime import (
    init as init_runtime,
)
from omnigent.runtime import (
    set_harness_process_manager,
    set_runner_id,
    set_runner_router,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests.unit.runner.helpers import NullServerClient
from tests.unit.runtime.harnesses._test_scaffold_harnesses import _EchoHarness

# ``claude-sdk`` is one of the harness names the agent spec validator
# accepts. The fixture rewrites its registry entry to point at the
# echo-harness scaffold module for the duration of the test (then
# restores) so ``_create_executor`` picks the runner client path
# without trying to import the real claude-sdk module.
_TEST_HARNESS_NAME = "claude-sdk"
_RUNNER_ID = "runner-tunnel-three-layer-test"


class FakeProcessManager:
    """In-process replacement for :class:`HarnessProcessManager`.

    Returns a per-conversation ``httpx.AsyncClient`` whose transport
    is an ``ASGITransport`` to a freshly-built :class:`_EchoHarness`
    FastAPI app with ``app.state.conversation_id`` pinned to that
    conversation. Mirrors the parts of the production process
    manager that the runner and ``the harness HTTP client`` actually
    call.

    Duplicates :class:`tests.unit.server.integration.test_sessions_three_layer.FakeProcessManager`;
    factor into a shared helper module once a third consumer needs it.
    """

    handles_tool_dispatch = False

    def __init__(self) -> None:
        self._apps: dict[str, FastAPI] = {}
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._in_flight: dict[str, str] = {}

    async def start(self) -> None:  # pragma: no cover — protocol shim
        pass

    async def shutdown(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._apps.clear()
        self._clients.clear()

    async def get_client(
        self,
        conversation_id: str,
        harness: str,
        env: dict[str, str] | None = None,
    ) -> httpx.AsyncClient:
        if conversation_id not in self._clients:
            app = _EchoHarness().build()
            app.state.conversation_id = conversation_id
            self._apps[conversation_id] = app
            self._clients[conversation_id] = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://harness",
                timeout=httpx.Timeout(5.0, read=None),
            )
        return self._clients[conversation_id]

    def has_session(self, conversation_id: str) -> bool:
        return conversation_id in self._clients

    def has_active_turn(self, conversation_id: str) -> bool:
        return conversation_id in self._in_flight

    async def release(self, conversation_id: str) -> None:
        client = self._clients.pop(conversation_id, None)
        if client is not None:
            await client.aclose()
        self._apps.pop(conversation_id, None)
        self._in_flight.pop(conversation_id, None)

    async def forward_cancel(self, conversation_id: str) -> None:
        # EchoHarness completes synchronously; cancellation is not
        # exercised by the happy-path test. A future tunneled
        # cancellation test would extend EchoHarness to block on
        # an event the test can release after the cancel frame.
        del conversation_id


def _build_harness_agent_bundle() -> bytes:
    """Build an agent bundle that routes through the harness path.

    Uses ``executor.type='omnigent'`` with
    ``executor.harness=<TEST_HARNESS>`` so ``_create_executor`` in
    ``runtime/workflow.py`` routes to ``the harness HTTP client`` and
    thus through the runner → harness HTTP chain.

    :returns: tar.gz bytes with a single ``config.yaml``.
    """
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": "echo-tunnel-test",
        "executor": {
            "type": "omnigent",
            "config": {"harness": _TEST_HARNESS_NAME},
            "model": "test-model",
        },
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = yaml.dump(config).encode()
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ── Tunnel handshake / forwarder helpers ──────────────────


def _websocket_scope(
    path: str,
    *,
    client_host: str = "127.0.0.1",
) -> dict[str, object]:
    """Build a minimal ASGI WebSocket scope for the tunnel route."""
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": (client_host, 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


async def _connect_runner_tunnel(
    app: FastAPI,
    runner_id: str,
) -> ApplicationCommunicator:
    """Open an ASGI WebSocket against the runner tunnel route.

    :returns: A communicator already past ``websocket.accept``.
    """
    communicator = ApplicationCommunicator(
        app,
        _websocket_scope(f"/v1/runners/{runner_id}/tunnel"),
    )
    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=2.0)
    assert accepted["type"] == "websocket.accept", (
        f"Tunnel route did not accept the WS handshake; got {accepted!r}"
    )
    return communicator


async def _send_hello_and_wait(
    communicator: ApplicationCommunicator,
    app: FastAPI,
    runner_id: str,
    *,
    harnesses: list[str],
) -> None:
    """Send a HelloFrame and wait until the registry lists the runner."""
    hello = HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=list(harnesses),
        envs=["os_sandbox"],
    )
    await communicator.send_input(
        {"type": "websocket.receive", "text": encode_frame(hello)},
    )
    registry = app.state.tunnel_registry

    async def _registered() -> None:
        while registry.get(runner_id) is None:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_registered(), timeout=2.0)


async def _forward_requests_to_runner(
    communicator: ApplicationCommunicator,
    runner_app: FastAPI,
) -> None:
    """Pump frames from the tunnel into the runner ASGI app.

    Reads ``websocket.send`` outputs from the server side and decodes
    each ``RequestFrame``. Each request is dispatched in its own
    asyncio task so streaming responses (SSE relays) don't block
    concurrent requests — the production runner WS loop has the same
    shape. Frame writes back to the communicator are serialised
    behind ``_write_lock`` because ``ApplicationCommunicator.send_input``
    is not safe to call from multiple concurrent tasks.
    """
    pending: set[asyncio.Task[None]] = set()
    write_lock = asyncio.Lock()

    async def _send_text(payload: str) -> None:
        async with write_lock:
            await communicator.send_input(
                {"type": "websocket.receive", "text": payload},
            )

    try:
        while True:
            # ApplicationCommunicator.receive_output() defaults to a
            # 1-second timeout. Under xdist scheduling pressure in CI
            # the >=1s gap between WS frames is common (one xdist
            # worker holds the event loop while EchoHarness runs), so
            # the default triggers asyncio.TimeoutError which kills
            # the forwarder and tears down the tunnel mid-turn. That
            # severs the AP-side SSE relay before it has persisted the
            # assistant message, and the test fails with "no assistant
            # text in session snapshot". 60s is well above any
            # plausible per-frame interval but still bounded so a
            # genuinely stuck test fails rather than hangs.
            output = await communicator.receive_output(timeout=60.0)
            if output["type"] == "websocket.close":
                return
            if output["type"] != "websocket.send":
                continue

            text = output.get("text")
            if not isinstance(text, str):
                continue

            frame = decode_frame(text)
            if not isinstance(frame, RequestFrame):
                continue

            task = asyncio.create_task(
                dispatch_via_asgi(runner_app, frame, _send_text),
                name=f"tunnel-dispatch-{frame.id}",
            )
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        for task in list(pending):
            task.cancel()
        for task in list(pending):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


# ── Fixture: Omnigent app + tunnel WS + runner app + EchoHarness ────


@dataclass
class _TunnelStack:
    ap_client: httpx.AsyncClient
    ap_app: FastAPI
    fake_pm: FakeProcessManager


@pytest_asyncio.fixture()
async def tunnel_three_layer_stack(tmp_path: Path) -> AsyncIterator[_TunnelStack]:
    """Wire Omnigent server + WS-tunneled runner + EchoHarness in-process.

    Lifecycle: build stores, init runtime, override the test harness
    module entry, build Omnigent app + runner app, open the WS tunnel via
    ``ApplicationCommunicator``, send hello, start the forwarder
    task, register the runner in the AP-side ``set_runner_router``
    so resource paths can resolve it, then yield an
    ``httpx.AsyncClient`` to the AP.

    Teardown shuts down in reverse order (httpx → forwarder →
    communicator → fake PM → runtime globals → DBOS) so DBOS
    background threads observe a clean exit before pytest-asyncio
    closes the loop.
    """
    db_uri = f"sqlite:///{tmp_path / 'test.db'}"

    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(
        artifact_store=artifact_store,
        cache_dir=tmp_path / "cache",
    )

    init_runtime(
        conversation_store=conv_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
    )

    # Override the harness module entry so ``_create_executor`` routes
    # ``claude-sdk`` to the EchoHarness scaffold for this test only.
    _saved_harness_module = _HARNESS_MODULES.get(_TEST_HARNESS_NAME)
    _HARNESS_MODULES[_TEST_HARNESS_NAME] = "tests.unit.runtime.harnesses._test_scaffold_harnesses"

    fake_pm = FakeProcessManager()
    set_harness_process_manager(fake_pm)
    set_runner_id(_RUNNER_ID)

    async def _resolve_spec(agent_id: str, session_id: str | None = None) -> Any:
        record = agent_store.get(agent_id)
        if record is None:
            return None
        return agent_cache.load(agent_id, record.bundle_location).spec

    runner_app = create_runner_app(
        process_manager=fake_pm,
        spec_resolver=_resolve_spec,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # Build the Omnigent server. ``create_app`` constructs the
    # ``TunnelRegistry`` + ``RunnerRouter`` synchronously (before
    # lifespan) and threads ``runner_router`` into every router as a
    # closure, so the WS route + the sessions route share the same
    # registry without us needing to drive the lifespan context
    # manager. We still publish the router into the runtime globals
    # so paths like ``_get_runner_client_for_resource_access`` (which
    # reads ``get_runner_router()``) see the same instance.
    ap_app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conv_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
    )
    set_runner_router(ap_app.state.runner_router)

    communicator = await _connect_runner_tunnel(ap_app, _RUNNER_ID)
    await _send_hello_and_wait(
        communicator,
        ap_app,
        _RUNNER_ID,
        harnesses=[_TEST_HARNESS_NAME],
    )

    forwarder_task = asyncio.create_task(
        _forward_requests_to_runner(communicator, runner_app),
        name="tunnel-three-layer-forwarder",
    )

    ap_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ap_app),
        base_url="http://ap",
        timeout=httpx.Timeout(10.0, read=None),
    )

    try:
        yield _TunnelStack(ap_client=ap_client, ap_app=ap_app, fake_pm=fake_pm)
    finally:
        # Cancel any AP-side background tasks (SSE relays, etc.) the
        # production lifespan would have owned. ``create_app`` does not
        # track them itself, so we sweep the loop for tasks whose
        # ``get_name()`` advertises a runner-relay coroutine and cancel
        # them before tearing down the tunnel. Without this, an
        # in-flight ``runner-relay-*`` task races the WS disconnect and
        # surfaces ``ConnectionError: tunnel closed`` as an uncaught
        # task exception during teardown.
        loop_tasks = []
        for task in asyncio.all_tasks():
            if task is asyncio.current_task():
                continue
            coro_qualname = getattr(task.get_coro(), "__qualname__", "")
            if (
                task.get_name().startswith("runner-relay-")
                or "_relay_runner_stream" in coro_qualname
            ):
                loop_tasks.append(task)
        for t in loop_tasks:
            t.cancel()
        for t in loop_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

        await ap_client.aclose()

        forwarder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await forwarder_task

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await communicator.send_input(
                {"type": "websocket.disconnect", "code": 1000},
            )
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await communicator.wait(timeout=2.0)

        # Close any cached WSTunnelTransport-backed clients before
        # we tear the registry down so they can flush in-flight
        # response futures cleanly.
        with contextlib.suppress(Exception):
            await ap_app.state.runner_router.aclose()

        await fake_pm.shutdown()
        set_runner_router(None)
        set_runner_id(None)
        set_harness_process_manager(None)
        if _saved_harness_module is None:
            _HARNESS_MODULES.pop(_TEST_HARNESS_NAME, None)
        else:
            _HARNESS_MODULES[_TEST_HARNESS_NAME] = _saved_harness_module


def _fake_client(ap_client: httpx.AsyncClient) -> object:
    """Build the SDK namespace shim expected by the REPL adapter.

    The sessions adapter only uses these namespace attributes; using
    the real namespace classes keeps request/response parsing on the
    production client path while avoiding an actual network client.

    :param ap_client: ASGI-backed client pointed at the Omnigent app.
    :returns: Duck-typed client with the namespaces the adapter uses.
    """
    base = str(ap_client.base_url).rstrip("/")

    class _FakeClient:
        """Duck-typed Omnigent client backed by the tunnel-stack httpx client."""

        def __init__(self) -> None:
            self.sessions = SessionsNamespace(ap_client, base)
            self.files = FilesNamespace(ap_client, base)

    return _FakeClient()


def _new_repl_adapter(
    ap_client: httpx.AsyncClient,
    *,
    agent_name: str,
    session_id: str | None = None,
    runner_id: str = _RUNNER_ID,
    runner_recover: Any | None = None,
) -> _SessionsChatReplAdapter:
    """Create a sessions REPL adapter over the tunneled Omnigent stack.

    :param ap_client: ASGI-backed client pointed at the Omnigent app.
    :param agent_name: Human-readable agent display name.
    :param session_id: Optional existing session to resume.
    :param runner_id: Runner id the adapter should bind before send.
    :param runner_recover: Optional callback returning replacement runner id.
    :returns: Configured :class:`_SessionsChatReplAdapter`.
    """
    return _SessionsChatReplAdapter(
        client=_fake_client(ap_client),  # type: ignore[arg-type]
        agent_name=agent_name,
        session_id=session_id,
        session_bundle=None if session_id is not None else _build_harness_agent_bundle(),
        runner_id=runner_id,
        runner_recover=runner_recover,
    )


async def _collect_adapter_turn(
    adapter: _SessionsChatReplAdapter,
    text: str,
) -> list[object]:
    """Send one adapter turn and collect terminal events.

    ``_SessionsChatReplAdapter.send`` intentionally yields only the
    terminal SDK-shaped event; streaming deltas render through the
    background callback path and are covered by the smoke test in this
    file plus the legacy adapter file.

    :param adapter: Adapter under test.
    :param text: User input for :meth:`_SessionsChatReplAdapter.send`.
    :returns: Events yielded by the ``send()`` generator.
    """
    send_events: list[object] = []
    async for ev in adapter.send(text):
        send_events.append(ev)
        if isinstance(ev, (ResponseCompleted, ResponseFailed)):
            break
    return send_events


# ── Tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_native_session_happy_path_via_ws_tunnel(
    tunnel_three_layer_stack: _TunnelStack,
) -> None:
    """End-to-end native sessions flow through the live WS tunnel.

    Mirrors ``test_session_happy_path_through_three_layers`` but
    forces traffic across the production tunnel route. Failure here
    points at either the AP↔tunnel wire (frame encoding, registry
    handshake) or the runner ASGI dispatcher — both invisible to the
    pure-ASGI three-layer test.
    """
    ap_client = tunnel_three_layer_stack.ap_client
    bundle = _build_harness_agent_bundle()

    create_resp = await ap_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert create_resp.status_code == 201, (
        f"session create failed: {create_resp.status_code} {create_resp.text}"
    )
    session_id = create_resp.json()["session_id"]

    bind_resp = await ap_client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": _RUNNER_ID},
    )
    assert bind_resp.status_code == 200, (
        f"runner bind failed: {bind_resp.status_code} {bind_resp.text}"
    )

    event_resp = await ap_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "ping"}],
            },
        },
    )
    assert event_resp.status_code == 202, (
        f"message event failed: {event_resp.status_code} {event_resp.text}"
    )

    # Poll until the relay has persisted the assistant message and
    # the cache reflects a terminal status. Checking status alone is
    # not enough: ``_get_session_snapshot`` defaults to ``"idle"`` on
    # a cache miss when the legacy ``get_runner_client()`` singleton
    # is unset (which is the case in any router-only deployment,
    # including this fixture). On a cold-cache run the relay can
    # still be in its initial transport handshake when the test
    # polls, so the first GET returns the default ``"idle"`` and the
    # old loop exited before the harness had emitted a single
    # delta, turning into a flaky "no assistant text" failure.
    # Waiting for the persisted assistant item makes us cancel-race
    # resistant too: the fixture's ``failed`` to ``idle`` rewrite
    # can no longer let us bail on a turn the runner never finished.
    # 600 × 100 ms = 60 s budget, well above the WS-frame round-trip
    # overhead but generous enough to absorb CI scheduler jitter.
    def _assistant_texts(snapshot: dict[str, Any]) -> list[str]:
        return [
            chunk.get("text", "")
            for item in snapshot["items"]
            if item["type"] == "message" and item["data"]["role"] == "assistant"
            for chunk in item["data"]["content"]
            if chunk.get("type") == "output_text"
        ]

    snap: dict[str, Any] | None = None
    final_status: str | None = None
    for _ in range(600):
        snap_resp = await ap_client.get(f"/v1/sessions/{session_id}")
        snap = snap_resp.json()
        final_status = snap["status"]
        if final_status == "failed":
            break
        if final_status == "idle" and _assistant_texts(snap):
            break
        await asyncio.sleep(0.1)

    assert snap is not None
    assert final_status == "idle", (
        f"session did not reach idle (got {final_status!r}); snapshot={json.dumps(snap, indent=2)}"
    )

    assistant_texts = _assistant_texts(snap)
    assert assistant_texts, (
        f"no assistant text in session snapshot; items={json.dumps(snap['items'], indent=2)}"
    )
    joined = " ".join(assistant_texts)
    assert "ping" in joined, f"echoed input missing from assistant text: {joined!r}"


@pytest.mark.asyncio
async def test_runner_router_uses_ws_tunnel_for_session_resources(
    tunnel_three_layer_stack: _TunnelStack,
) -> None:
    """Resource-access lookups go through the same WS-tunneled client.

    Lightweight REPL-adapter-shaped smoke: after binding, the AP's
    runner router must hand out a ``WSTunnelTransport``-backed client
    (not a fallback ASGI one) for the session. This catches the
    regression class where ``_get_runner_client_for_resource_access``
    falls back to ``get_runner_client()`` because the router lookup
    silently returned ``None``.
    """
    ap_client = tunnel_three_layer_stack.ap_client
    ap_app = tunnel_three_layer_stack.ap_app

    create_resp = await ap_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _build_harness_agent_bundle(),
                "application/gzip",
            ),
        },
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["session_id"]

    bind_resp = await ap_client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": _RUNNER_ID},
    )
    assert bind_resp.status_code == 200

    routed = ap_app.state.runner_router.client_for_session_resources(session_id)
    assert routed.runner_id == _RUNNER_ID, (
        f"router did not pin to {_RUNNER_ID!r}; got {routed.runner_id!r}"
    )

    transport = routed.client._transport
    assert isinstance(transport, WSTunnelTransport), (
        "router handed out a non-tunnel transport; tunnel wiring "
        f"regressed. Got {type(transport).__name__}."
    )


@pytest.mark.asyncio
async def test_repl_adapter_smoke_via_ws_tunnel(
    tunnel_three_layer_stack: _TunnelStack,
) -> None:
    """REPL adapter ``send()`` rides the same tunnel and yields terminal.

    Lightweight smoke that the legacy adapter still completes its
    end-to-end loop when the AP↔runner hop is the production WS
    tunnel rather than ASGITransport. Builds the same client-namespace
    shim ``test_sessions_repl_adapter.py`` uses (typed-duck, not the
    real SDK ``Client``) so we don't pull a network dependency and
    don't import a private helper across test files.

    Asserts a terminal completion arrives — the EchoHarness emits a
    ``response.output_text.delta`` then completes. A failed terminal is
    not accepted here because this test is intended to catch tunnel
    regressions, not merely prove that the adapter can surface them.
    """
    adapter = _new_repl_adapter(
        tunnel_three_layer_stack.ap_client,
        agent_name="tunnel-smoke-agent",
    )

    try:
        send_events = await _collect_adapter_turn(adapter, "ping")

        assert not any(isinstance(e, ResponseFailed) for e in send_events), (
            "adapter.send failed over the tunnel; "
            f"events={[type(e).__name__ for e in send_events]}"
        )
        assert any(isinstance(e, ResponseCompleted) for e in send_events), (
            "adapter.send did not yield ResponseCompleted over the tunnel; "
            f"events={[type(e).__name__ for e in send_events]}"
        )
    finally:
        with contextlib.suppress(Exception):
            await adapter.aclose()


@pytest.mark.asyncio
async def test_repl_adapter_resume_rebinds_via_ws_tunnel(
    tunnel_three_layer_stack: _TunnelStack,
) -> None:
    """Resumed REPL adapter rebinding works on the native tunnel path.

    Starts from an existing session bound to the live runner, then
    resumes through an adapter whose local ``_bound_runner_id`` cache
    is stale. The send must PATCH the session before dispatching over
    the tunnel; otherwise the user message would not reach the runner
    or the adapter would skip the resume binding path entirely.
    """
    ap_client = tunnel_three_layer_stack.ap_client

    create_resp = await ap_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _build_harness_agent_bundle(),
                "application/gzip",
            ),
        },
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["session_id"]

    bind_resp = await ap_client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": _RUNNER_ID},
    )
    assert bind_resp.status_code == 200

    adapter = _new_repl_adapter(
        ap_client,
        agent_name="tunnel-resume-agent",
        session_id=session_id,
        runner_id=_RUNNER_ID,
    )
    adapter._bound_runner_id = "stale-runner-cache"

    try:
        send_events = await _collect_adapter_turn(adapter, "resumed turn")
        assert not any(isinstance(e, ResponseFailed) for e in send_events), (
            f"resumed turn failed; events={[type(e).__name__ for e in send_events]}"
        )
        assert any(isinstance(e, ResponseCompleted) for e in send_events), (
            f"resumed turn did not complete; events={[type(e).__name__ for e in send_events]}"
        )
        assert adapter.session_id == session_id
        assert adapter._bound_runner_id == _RUNNER_ID

        snap_resp = await ap_client.get(f"/v1/sessions/{session_id}")
        assert snap_resp.status_code == 200
        snap = snap_resp.json()
        assert snap["runner_id"] == _RUNNER_ID
        user_texts = [
            block["text"]
            for item in snap["items"]
            if item.get("type") == "message"
            and isinstance(item.get("data"), dict)
            and item["data"].get("role") == "user"
            for block in item["data"].get("content", [])
            if isinstance(block, dict) and block.get("type") == "input_text"
        ]
        assert "resumed turn" in user_texts, (
            f"resumed input missing from snapshot: {json.dumps(snap['items'], indent=2)}"
        )
    finally:
        with contextlib.suppress(Exception):
            await adapter.aclose()


@pytest.mark.asyncio
async def test_on_runner_connect_restarts_relay_via_router(
    tunnel_three_layer_stack: _TunnelStack,
) -> None:
    """Reconnect hook restarts relays via the router.

    Pre-fix, ``_on_runner_connect`` short-circuited on
    ``get_runner_client() is None`` — always true in multi-runner
    deployments where only ``set_runner_router`` is wired — so every
    reconnect was a silent no-op.

    Drives a real WS tunnel disconnect/reconnect and asserts the hook
    (a) routes through ``client_for_session_resources`` for the bound
    session, (b) reaches ``_ensure_runner_relay`` with the exact
    routed client, and (c) leaves a sibling session bound to a
    different runner alone (per-conv ``runner_id`` filter).
    """
    from omnigent.runner.routing import RoutedRunner
    from omnigent.server.routes import sessions as sessions_routes
    from omnigent.server.routes.sessions import _runner_relay_tasks

    ap_client = tunnel_three_layer_stack.ap_client
    ap_app = tunnel_three_layer_stack.ap_app
    fake_pm = tunnel_three_layer_stack.fake_pm

    create_resp = await ap_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _build_harness_agent_bundle(),
                "application/gzip",
            ),
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["session_id"]

    bind_resp = await ap_client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": _RUNNER_ID},
    )
    assert bind_resp.status_code == 200, bind_resp.text

    # Sibling session bound to a different runner so the per-conv
    # ``runner_id`` filter has something to skip. Written via the
    # store directly because PATCH-bind would spawn a relay against
    # a runner that has no WS pump, hanging teardown.
    from omnigent.runtime import get_conversation_store

    other_runner_id = "runner-other-irrelevant"
    other_create_resp = await ap_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _build_harness_agent_bundle(),
                "application/gzip",
            ),
        },
    )
    assert other_create_resp.status_code == 201, other_create_resp.text
    other_session_id = other_create_resp.json()["session_id"]
    get_conversation_store().replace_runner_id(other_session_id, other_runner_id)

    initial_handle = _runner_relay_tasks.get(session_id)
    assert initial_handle is not None, (
        "PATCH-bind did not spawn a relay; the reconnect assertion "
        "below would be vacuous without a baseline relay to replace."
    )
    initial_task = initial_handle.task

    # Spies on the router resolver and ``_ensure_runner_relay``.
    # The stub ``.post`` short-circuits because ``WSTunnelTransport``
    # ignores httpx ``timeout=`` and would hang. The
    # ensure spy does not chain through — the real helper would spawn
    # an SSE task that errors immediately against the stub client.
    router = ap_app.state.runner_router
    real_resolver = router.client_for_session_resources
    routed_calls: list[str] = []
    routed_clients: dict[str, Any] = {}

    class _StubResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class _StubClient:
        async def post(self, *args: Any, **kwargs: Any) -> _StubResponse:
            return _StubResponse()

    def _spy_resolver(conv_id: str):  # type: ignore[no-untyped-def]
        routed_calls.append(conv_id)
        real_routed = real_resolver(conv_id)
        fake = RoutedRunner(runner_id=real_routed.runner_id, client=_StubClient())  # type: ignore[arg-type]
        routed_clients[conv_id] = fake.client
        return fake

    router.client_for_session_resources = _spy_resolver  # type: ignore[method-assign]

    real_ensure = sessions_routes._ensure_runner_relay
    ensure_calls: list[tuple[str, str | None, Any]] = []

    def _spy_ensure(sid, rid, client, store=None):  # type: ignore[no-untyped-def]
        ensure_calls.append((sid, rid, client))

    sessions_routes._ensure_runner_relay = _spy_ensure  # type: ignore[assignment]

    new_communicator: ApplicationCommunicator | None = None
    new_forwarder_task: asyncio.Task[None] | None = None
    try:
        # Deregister aborts the in-flight relay with ConnectionError;
        # the relay's done-callback then clears its slot.
        ap_app.state.tunnel_registry.deregister(_RUNNER_ID)

        async def _relay_slot_cleared() -> None:
            while True:
                handle = _runner_relay_tasks.get(session_id)
                if handle is None or handle.task.done():
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_relay_slot_cleared(), timeout=2.0)

        # Fresh WS + hello re-registers and fires _on_runner_connect.
        new_communicator = await _connect_runner_tunnel(ap_app, _RUNNER_ID)
        await _send_hello_and_wait(
            new_communicator,
            ap_app,
            _RUNNER_ID,
            harnesses=[_TEST_HARNESS_NAME],
        )

        async def _resolve_spec(agent_id: str) -> Any:
            return None

        new_runner_app = create_runner_app(
            process_manager=fake_pm,
            spec_resolver=_resolve_spec,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        new_forwarder_task = asyncio.create_task(
            _forward_requests_to_runner(new_communicator, new_runner_app),
            name="tunnel-reconnect-forwarder",
        )

        async def _hook_did_its_job() -> None:
            while True:
                if session_id in routed_calls and any(
                    sid == session_id for sid, _, _ in ensure_calls
                ):
                    return
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(_hook_did_its_job(), timeout=5.0)
        except asyncio.TimeoutError:
            registry_entry = ap_app.state.tunnel_registry.get(_RUNNER_ID)
            raise AssertionError(
                "Reconnect did not drive the connect hook to completion within 5s.\n"
                f"  routed_calls = {routed_calls}\n"
                f"  ensure_calls = {ensure_calls}\n"
                f"  registry has runner = {registry_entry is not None}\n"
                f"  initial_task.done = {initial_task.done()}"
            ) from None

        assert session_id in routed_calls, (
            "_on_runner_connect did not call "
            "runner_router.client_for_session_resources for the bound "
            "session — legacy get_runner_client() path is still in use "
            "(B2 regression)."
        )
        ensure_for_session = [
            (sid, rid, client) for sid, rid, client in ensure_calls if sid == session_id
        ]
        assert ensure_for_session, (
            "_on_runner_connect resolved the router but never reached "
            "_ensure_runner_relay for the bound session."
        )
        _last_sid, last_rid, last_client = ensure_for_session[-1]
        assert last_rid == _RUNNER_ID, (
            f"_ensure_runner_relay was called with runner_id "
            f"{last_rid!r}, expected {_RUNNER_ID!r}."
        )
        assert last_client is routed_clients[session_id], (
            "_ensure_runner_relay was called with a client that is not the "
            "one returned by runner_router.client_for_session_resources — "
            "the routed client is not threading through the hook."
        )

        # Sibling session bound to a different runner must not be touched
        # — the per-conv runner_id filter is the only cross-runner guard.
        assert other_session_id not in routed_calls, (
            f"Connect hook resolved router for sibling session "
            f"{other_session_id!r} bound to {other_runner_id!r} — "
            f"per-conv runner_id filter regressed."
        )
        assert all(sid != other_session_id for sid, _, _ in ensure_calls), (
            f"Connect hook called _ensure_runner_relay for sibling session "
            f"{other_session_id!r} bound to {other_runner_id!r}."
        )
    finally:
        router.client_for_session_resources = real_resolver  # type: ignore[method-assign]
        sessions_routes._ensure_runner_relay = real_ensure  # type: ignore[assignment]
        if new_forwarder_task is not None:
            new_forwarder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await new_forwarder_task
        if new_communicator is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await new_communicator.send_input(
                    {"type": "websocket.disconnect", "code": 1000},
                )
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await new_communicator.wait(timeout=2.0)


# TODO: factor ``FakeProcessManager`` and ``_build_harness_agent_bundle``
# out of ``test_sessions_three_layer.py`` + this file into a shared
# ``_three_layer_helpers.py`` module once a third caller arrives. Kept
# duplicated for now to keep this PR scoped to fixture + tests only.
