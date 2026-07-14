"""Integration tests for the inline host-launch path of ``POST /v1/sessions``.

This is the path the Web UI's New Chat wizard actually hits: the
browser picks a host + workspace + agent and POSTs them to
``/v1/sessions`` in one call, and the server validates the workspace
against the host (``host.stat``), atomically binds a runner, and
sends a ``host.launch_runner`` frame.

The dedicated ``POST /v1/hosts/{id}/runners`` endpoint is covered by
``test_hosts_api.py``. These tests pin the *inline* path's distinct
behavior — in particular its deliberately lenient failure handling:
unlike the dedicated endpoint (which raises 502/504 when the host
declines or times out), session-create still returns 201 with the
runner bound, leaving the session recoverable via reconnect/relaunch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import httpx
import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    HostStopRunnerFrame,
    HostStopRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_weblaunch_test"
_WORKSPACE = "/work/repo"


class _NoopRunnerWS:
    """Minimal runner WebSocket fake for registering a tunnel session."""

    async def send_text(self, data: str) -> None:
        """Accept outbound tunnel frames without sending them anywhere."""
        del data

    async def receive_text(self) -> str:
        """Block forever; tests do not drive runner inbound frames."""
        return await asyncio.Future()


def _runner_hello() -> HelloFrame:
    """Build a runner hello frame for test tunnel registrations.

    :returns: Hello frame with generic native harness capabilities.
    """
    return HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=[
            "claude-native",
            "claude-sdk",
            "codex",
            "codex-native",
            "openai-agents",
        ],
        envs=[],
    )


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path) -> FastAPI:
    """FastAPI app wired WITH ``host_store`` so the inline host-launch
    branch of ``POST /v1/sessions`` is active.

    Overrides the ``app`` fixture from ``tests/server/conftest.py``
    (which passes ``host_store=None`` and therefore skips the launch
    branch). The shared ``client`` fixture depends on this ``app`` and
    so resolves to this override for tests in this module.

    :param runtime_init: Initializes the runtime + mock LLM.
    :param db_uri: SQLite database URI.
    :param tmp_path: Pytest temp dir for artifacts and cache.
    :returns: A configured FastAPI app with host routes mounted.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
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


def _websocket_scope(path: str) -> dict[str, object]:
    """Build a minimal ASGI WebSocket scope for the host tunnel.

    :param path: WebSocket path, e.g. ``"/v1/hosts/<id>/tunnel"``.
    :returns: ASGI WebSocket scope dict.
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


async def _connect_host(app: FastAPI) -> ApplicationCommunicator:
    """Connect a mock host over the WebSocket tunnel and wait for it
    to register in the app's host registry.

    :param app: The FastAPI app under test (its ``state.host_registry``
        is the same instance the session-create launch path reads).
    :returns: The connected ASGI communicator, ready to exchange frames.
    """
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"

    hello = encode_host_frame(
        HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="laptop")
    )
    await comm.send_input({"type": "websocket.receive", "text": hello})
    registry = app.state.host_registry
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)
    return comm


async def _wait_for_runner_connect_waiter(
    app: FastAPI,
    runner_id: str,
    *,
    timeout_s: float = 1.0,
) -> None:
    """Wait until the app registry has a connect waiter for a runner.

    :param app: FastAPI app whose ``state.tunnel_registry`` is under
        test.
    :param runner_id: Runner id expected to have one waiter, e.g.
        ``"runner_token_abc123"``.
    :param timeout_s: Maximum seconds to wait, e.g. ``1.0``.
    :returns: None.
    :raises AssertionError: If no waiter appears before the timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if app.state.tunnel_registry.connect_waiter_count(runner_id) > 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"runner {runner_id!r} did not get a connect waiter")


async def _serve_one_launch(
    comm: ApplicationCommunicator,
    *,
    launch_status: str,
    launch_error: str | None = None,
    launch_error_code: str | None = None,
) -> HostLaunchRunnerFrame:
    """Answer the host round-trips for a single inline session launch.

    The inline path performs two sequential host round-trips within
    the one ``POST /v1/sessions`` call: a ``host.stat`` to validate
    the workspace, then a ``host.launch_runner``. This reads the
    host's outbound frames, replies "exists/directory" to the stat
    (echoing the requested path as the canonical path) and replies
    with ``launch_status`` to the launch, then returns.

    :param comm: The connected host communicator.
    :param launch_status: ``"launched"`` or ``"failed"`` — the status
        the fake host reports for the launch.
    :param launch_error: Error string to attach when the launch is
        reported as failed.
    :param launch_error_code: Structured failure category to attach,
        e.g. ``"harness_not_configured"``; ``None`` reports an
        uncategorized failure (legacy host behavior).
    :returns: The ``host.launch_runner`` frame the server sent, so
        callers can assert on its fields (e.g. ``harness``).
    """
    # Bounded so a routing bug can't hang the test: stat + launch are
    # 2 frames, the rest of the budget absorbs interleaved pings.
    for _ in range(40):
        output = await comm.receive_output(timeout=3.0)
        if output["type"] != "websocket.send":
            continue
        frame = decode_host_frame(output["text"])
        if isinstance(frame, HostStatFrame):
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostStatResultFrame(
                            request_id=frame.request_id,
                            status="ok",
                            exists=True,
                            type="directory",
                            canonical_path=frame.path,
                        )
                    ),
                }
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostLaunchRunnerResultFrame(
                            request_id=frame.request_id,
                            status=launch_status,
                            runner_id="runner_from_host" if launch_status == "launched" else None,
                            error=launch_error,
                            error_code=launch_error_code,
                        )
                    ),
                }
            )
            return frame
    raise AssertionError("host never received a launch frame from the inline path")


async def _serve_one_stop(comm: ApplicationCommunicator) -> str:
    """Answer the host's ``host.stop_runner`` round-trip for one Stop.

    Reads the host's outbound frames until a
    :class:`HostStopRunnerFrame` arrives (skipping interleaved pings),
    replies ``stopped`` so the server's pending-stop future resolves,
    and returns the ``runner_id`` the server asked the host to stop.

    :param comm: The connected host communicator.
    :returns: The ``runner_id`` carried by the stop frame, e.g.
        ``"runner_token_abc123..."``.
    :raises AssertionError: If no stop frame arrives within the frame
        budget — i.e. the Stop handler did not ask the host to stop the
        runner (the regression this guards against).
    """
    # Bounded so a missing stop frame fails fast instead of hanging.
    for _ in range(40):
        output = await comm.receive_output(timeout=3.0)
        if output["type"] != "websocket.send":
            continue
        frame = decode_host_frame(output["text"])
        if isinstance(frame, HostStopRunnerFrame):
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostStopRunnerResultFrame(
                            request_id=frame.request_id,
                            status="stopped",
                        )
                    ),
                }
            )
            return frame.runner_id
    raise AssertionError("host never received a stop_runner frame from the stop path")


async def _expect_no_launch(comm: ApplicationCommunicator, *, budget_s: float) -> bool:
    """Watch host outbound frames for a launch frame within a budget.

    Reads the host's outbound frames (skipping interleaved pings) until
    either a :class:`HostLaunchRunnerFrame` arrives or the receive blocks
    longer than *budget_s* with nothing more to read.

    :param comm: Connected host communicator.
    :param budget_s: Seconds to wait on each receive before concluding no
        further frames are coming, e.g. ``2.0``.
    :returns: ``True`` if a launch frame was observed (a relaunch was
        attempted), ``False`` if none arrived within the budget (the
        expected outcome for a deliberately stopped session).
    """
    try:
        # Bounded so a chatty ping loop can't spin forever.
        for _ in range(40):
            output = await comm.receive_output(timeout=budget_s)
            if output["type"] != "websocket.send":
                continue
            if isinstance(decode_host_frame(output["text"]), HostLaunchRunnerFrame):
                return True
    except asyncio.TimeoutError:
        return False
    return False


async def _wait_for_launch(
    comm: ApplicationCommunicator, *, budget_s: float
) -> HostLaunchRunnerFrame | None:
    """Watch host outbound frames and return the first launch frame seen.

    The positive counterpart to :func:`_expect_no_launch`: reads the
    host's outbound frames (skipping interleaved pings) and returns the
    :class:`HostLaunchRunnerFrame` if one arrives within *budget_s*, or
    ``None`` if none does within the budget.

    :param comm: Connected host communicator.
    :param budget_s: Seconds to wait on each receive before concluding no
        launch is coming, e.g. ``5.0``.
    :returns: The launch frame the host received, or ``None``.
    """
    try:
        # Bounded so a chatty ping loop can't spin forever.
        for _ in range(40):
            output = await comm.receive_output(timeout=budget_s)
            if output["type"] != "websocket.send":
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostLaunchRunnerFrame):
                return frame
    except asyncio.TimeoutError:
        return None
    return None


async def test_inline_launch_binds_runner_and_returns_host(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    """Happy path: ``POST /v1/sessions`` with ``host_id`` + ``workspace``
    validates the workspace, binds a runner, launches it, and returns
    the binding.

    A failure here means the Web UI New Chat wizard's create call no
    longer launches a runner — the user would get an unbound session.
    """
    comm = await _connect_host(app)
    agent = await create_test_agent(client)

    responder = asyncio.create_task(_serve_one_launch(comm, launch_status="launched"))
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_id": _HOST_ID, "workspace": _WORKSPACE},
    )
    await responder

    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
    body = resp.json()
    # The response must carry the binding so the UI can route to the
    # runner; a None here means the launch branch didn't run.
    assert body["host_id"] == _HOST_ID
    assert body["runner_id"] is not None
    # runner_id is derived from the server's binding token, NOT the
    # runner_id the host echoed back — so it has the token prefix.
    assert body["runner_id"].startswith("runner_token_"), (
        f"runner_id should be derived from the server binding token, got {body['runner_id']!r}"
    )
    # Canonical workspace from host.stat is what gets stored/returned.
    assert body["workspace"] == _WORKSPACE

    # The conversation row must reflect the same binding, proving the
    # atomic set_runner_id + host_id/workspace writes persisted.
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(body["id"])
    assert conv is not None
    assert conv.runner_id == body["runner_id"]
    assert conv.host_id == _HOST_ID
    assert conv.workspace == _WORKSPACE


async def test_inline_launch_failure_still_returns_bound_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    """When the host reports the launch failed, the inline path still
    returns 201 with the runner bound — its deliberately lenient
    contract, distinct from ``POST /v1/hosts/{id}/runners`` (which
    raises 502 on the same host failure).

    If this starts returning an error status, the divergence with the
    dedicated launch endpoint has changed and the UI's create flow may
    surface a hard failure for a session that is actually recoverable.
    """
    comm = await _connect_host(app)
    agent = await create_test_agent(client)

    responder = asyncio.create_task(
        _serve_one_launch(comm, launch_status="failed", launch_error="disk full")
    )
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_id": _HOST_ID, "workspace": _WORKSPACE},
    )
    await responder

    # Lenient: launch failure does NOT fail the create.
    assert resp.status_code == 201, f"expected 201 despite launch failure, got {resp.status_code}"
    body = resp.json()
    assert body["host_id"] == _HOST_ID
    # The runner is bound even though the host declined: the session
    # row was atomically bound (with a real token-derived id) before
    # the launch frame was sent, and the failure path leaves it in
    # place for a later relaunch rather than rolling it back.
    assert body["runner_id"].startswith("runner_token_"), (
        f"a token-bound runner_id should be set despite launch failure, got {body['runner_id']!r}"
    )

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(body["id"])
    assert conv is not None
    assert conv.runner_id == body["runner_id"], (
        "runner binding should persist even when the host reports launch failure"
    )
    assert conv.host_id == _HOST_ID


_HARNESS_REFUSAL = (
    "harness 'codex' is not configured on host 'laptop' — run `omnigent setup` on that machine"
)


async def test_inline_create_harness_not_configured_stays_lenient(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    """A ``harness_not_configured`` refusal at CREATE is fully lenient.

    The picker's readiness data can be stale (the user may have run
    ``omnigent setup`` since the host last connected), so create never
    gates on it: the session opens (201), the binding is kept, and —
    unlike the earlier design — NO transcript item is written at create
    time. The error is deferred to the first-message relaunch (the real
    runner-start attempt), covered by the next test.

    If create starts persisting an item again, the premature
    before-you-typed-anything notice this iteration removed is back; if
    it stops returning 201 or clears the binding, the lenient/recoverable
    contract regressed.
    """
    comm = await _connect_host(app)
    agent = await create_test_agent(
        client,
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )

    responder = asyncio.create_task(
        _serve_one_launch(
            comm,
            launch_status="failed",
            launch_error=_HARNESS_REFUSAL,
            launch_error_code="harness_not_configured",
        )
    )
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_id": _HOST_ID, "workspace": _WORKSPACE},
    )
    launch_frame = await responder

    # The create-time launch carries the agent's canonical harness so the
    # host can run its check; the refusal is just handled leniently.
    assert launch_frame.harness == "codex", (
        f"create launch frame should carry the agent's harness, got {launch_frame.harness!r}"
    )
    assert resp.status_code == 201, f"expected 201, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["host_id"] == _HOST_ID
    assert body["runner_id"] is not None

    # No transcript item at create — the notice is deferred to the
    # first-message relaunch path.
    items = await client.get(f"/v1/sessions/{body['id']}/items")
    assert items.status_code == 200, items.text
    assert items.json()["data"] == [], (
        f"create must persist no transcript item for a refused launch, "
        f"got {items.json()['data']!r}"
    )


async def test_message_relaunch_harness_not_configured_persists_error_turn(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message whose host relaunch is refused persists user msg + error.

    The first message is the real runner-start attempt. When the host
    refuses the relaunch with ``harness_not_configured``, the server
    consumes the user message AND records a sibling ``type="error"`` item
    carrying the host's `omnigent setup` message (the web renders it as
    an error banner) — instead of timing out into a generic
    ``RUNNER_UNAVAILABLE``. The binding is left intact so a later message
    relaunches once the user has run setup.

    Mutation check: drop the ``harness_not_configured`` branch in
    post_event's relaunch and the message instead 503s with
    ``runner_unavailable`` and no error item is written — both assertions
    below fail.
    """
    from omnigent.runtime import set_runner_client
    from omnigent.server.routes import sessions as sessions_module

    # Grace=0 so the message takes the relaunch branch immediately instead
    # of waiting for the (never-connecting) create-bound runner.
    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0.0)

    comm = await _connect_host(app)
    agent = await create_test_agent(
        client,
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )
    # Create with a successful create-time launch so the session binds a
    # runner_id (the fake runner never actually connects).
    create_responder = asyncio.create_task(_serve_one_launch(comm, launch_status="launched"))
    create_resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_id": _HOST_ID, "workspace": _WORKSPACE},
    )
    await create_responder
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]

    # Runner offline (none ever connected) → the message relaunches; serve
    # that relaunch as a harness refusal.
    set_runner_client(None)
    relaunch_responder = asyncio.create_task(
        _serve_one_launch(
            comm,
            launch_status="failed",
            launch_error=_HARNESS_REFUSAL,
            launch_error_code="harness_not_configured",
        )
    )
    try:
        msg_resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
    finally:
        await relaunch_responder
        set_runner_client(None)

    # The message is accepted (not a 503 RUNNER_UNAVAILABLE): the server
    # consumed it and recorded the failure turn.
    assert msg_resp.status_code == 202, (
        f"expected 202, got {msg_resp.status_code}: {msg_resp.text}"
    )

    items = await client.get(f"/v1/sessions/{session_id}/items")
    assert items.status_code == 200, items.text
    data = items.json()["data"]
    # The user's message survives in the transcript.
    user_texts = [
        part.get("text", "")
        for item in data
        if item.get("type") == "message"
        for part in item.get("content", [])
    ]
    assert "hi" in user_texts, f"user message should be persisted, got {user_texts!r}"
    # A type="error" item carries the host's refusal (rendered as a banner),
    # including the remediation command.
    error_items = [item for item in data if item.get("type") == "error"]
    assert len(error_items) == 1, (
        f"expected exactly one error item for the refused relaunch, got {error_items!r}"
    )
    assert error_items[0]["code"] == "harness_not_configured"
    assert "omnigent setup" in error_items[0]["message"]
    assert "harness 'codex' is not configured" in error_items[0]["message"]

    # Binding kept so a post-setup message can relaunch.
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.host_id == _HOST_ID


@pytest.mark.parametrize(
    "workspace,expected_detail",
    [
        (None, "workspace required"),
        ("relative/path", "absolute path"),
    ],
)
async def test_inline_launch_rejects_bad_workspace(
    client: httpx.AsyncClient,
    workspace: str | None,
    expected_detail: str,
) -> None:
    """``host_id`` set with a missing or non-absolute ``workspace`` is
    rejected at the route with 400 before any host contact.

    This is the route-level contract behind the wizard's workspace
    picker (which only submits absolute paths). The lower-level
    ``validate_workspace`` unit tests don't cover the "workspace
    required when host_id is set" branch — that check lives in the
    session-create helper.
    """
    agent = await create_test_agent(client)
    payload: dict[str, object] = {"agent_id": agent["id"], "host_id": _HOST_ID}
    if workspace is not None:
        payload["workspace"] = workspace

    resp = await client.post("/v1/sessions", json=payload)

    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    assert expected_detail in resp.text


async def _inline_launch_session(
    client: httpx.AsyncClient, comm: ApplicationCommunicator
) -> dict[str, str]:
    """Inline-launch a host-bound session and return its id + runner_id.

    Drives ``POST /v1/sessions`` with ``host_id`` + ``workspace`` while
    answering the host's stat + launch round-trips, so the returned
    session has ``host_id`` and a token-bound ``runner_id`` persisted —
    the shape the Stop / health / relaunch paths read.

    :param client: Test HTTP client bound to the host-wired app.
    :param comm: Connected host communicator.
    :returns: ``{"id": <session id>, "runner_id": <token-bound id>}``.
    """
    agent = await create_test_agent(client)
    launch_responder = asyncio.create_task(_serve_one_launch(comm, launch_status="launched"))
    create_resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_id": _HOST_ID, "workspace": _WORKSPACE},
    )
    await launch_responder
    assert create_resp.status_code == 201, create_resp.text
    return {"id": create_resp.json()["id"], "runner_id": create_resp.json()["runner_id"]}


async def _stop_host_session(
    client: httpx.AsyncClient,
    comm: ApplicationCommunicator,
    session_id: str,
) -> str:
    """Drive ``stop_session`` and serve the host's stop_runner round-trip.

    Installs a fake global runner client that 204s the pane-kill forward
    (``_stop_session_via_runner`` falls back to it because no runner
    tunnel is registered in the host-only test app), POSTs the
    ``stop_session`` control event, and answers the host's
    ``host.stop_runner``.

    :param client: Test HTTP client.
    :param comm: Connected host communicator.
    :param session_id: Session to stop, e.g. ``"conv_abc123"``.
    :returns: The ``runner_id`` the host was told to stop.
    """
    from omnigent.runtime import set_runner_client

    def _runner_handler(request: httpx.Request) -> httpx.Response:
        """204 every runner POST (pane-kill forward) and snapshot GET."""
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_runner_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        stop_responder = asyncio.create_task(_serve_one_stop(comm))
        stop_resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "stop_session", "data": {}},
        )
        stopped_runner_id = await stop_responder
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    assert stop_resp.status_code == 202, stop_resp.text
    # stop_session is a control event, not a persisted item.
    assert stop_resp.json() == {"queued": False}, stop_resp.json()
    return stopped_runner_id


async def test_stop_session_stops_host_launched_runner(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """``stop_session`` on a host-launched session also stops the runner.

    Killing the claude pane alone leaves the host-launched runner
    connected. The server must additionally send the host a
    ``host.stop_runner`` for the session's bound runner so the runner's
    tunnel drops. This pins that end-to-end forward: a real host over the
    WS tunnel, a real inline-launched session (host_id + token-bound
    runner_id on the conv row), and an assertion that the host receives a
    stop frame targeting *that* runner. If the Stop handler stopped
    forwarding to the host, ``_serve_one_stop`` would never see the frame.
    """
    comm = await _connect_host(app)
    session = await _inline_launch_session(client, comm)

    stopped_runner_id = await _stop_host_session(client, comm, session["id"])

    # The host was asked to stop the SAME runner the session is bound to —
    # proving the Stop handler read host_id/runner_id off the session row
    # and forwarded a stop_runner for it. A wrong/empty id would mean the
    # teardown targeted the wrong runner (or didn't run).
    assert stopped_runner_id == session["runner_id"], (
        f"host should be told to stop the session's bound runner "
        f"{session['runner_id']!r}, got {stopped_runner_id!r}"
    )


async def test_stopped_host_session_writes_no_label_and_host_stays_online(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """After Stop, no marker is written and the host stays reachable.

    Stop is non-sticky (WS-S2): it kills the host-launched runner but
    writes NO persistent marker. With the host's ``omnigent host``
    tunnel still open, ``GET /health`` keeps reporting ``host_online:
    true`` — the relaunch affordance the open-session view needs — so the
    next message auto-relaunches via the normal dispatch path (covered by
    :func:`test_stopped_host_session_message_relaunches_runner`).

    Asserts the post-Stop liveness reports the host still online and that
    NO ``omnigent.stopped`` label is persisted. Mutation check: re-add a
    sticky stop-label write and the no-label assertion fails.
    """
    comm = await _connect_host(app)
    session = await _inline_launch_session(client, comm)
    session_id = session["id"]

    await _stop_host_session(client, comm, session_id)

    # The host's tunnel is still open, so host_online stays true: the
    # session is still relaunch-eligible on the next message.
    health = await client.get(f"/health?session_ids={session_id}")
    liveness = health.json()["sessions"][session_id]
    assert liveness["host_online"] is True, (
        "the host's tunnel is still open after Stop, so host_online must "
        "stay true so the next message can relaunch the runner"
    )

    # Stop is non-sticky: no persistent marker is written. A re-introduced
    # sticky label would resurface the retired omnigent.stopped behavior.
    snap = await client.get(f"/v1/sessions/{session_id}")
    assert "omnigent.stopped" not in snap.json()["labels"], (
        f"Stop must NOT persist any omnigent.stopped label; got {snap.json()['labels']!r}"
    )


async def test_stopped_host_session_message_relaunches_runner(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message to a stopped host session relaunches the runner.

    Stop is non-sticky (WS-S2): it kills the current runner but writes no
    marker, so a subsequent message must auto-relaunch the session on its
    still-online host via the normal message-dispatch relaunch path —
    exactly as if the runner had merely died. This is the behavior that
    replaces the retired ``resume_session`` machinery.

    Asserts that after Stop, posting a message sends the host a
    ``host.launch_runner`` and rotates the conversation ``runner_id`` to a
    fresh token-bound id. Mutation check: re-add the deliberate-stop guard
    to the relaunch branch and no launch frame is sent — the first
    assertion fails.
    """
    from omnigent.runtime import set_runner_client
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0.0)

    comm = await _connect_host(app)
    session = await _inline_launch_session(client, comm)
    session_id = session["id"]
    await _stop_host_session(client, comm, session_id)

    # After Stop the conversation still carries host_id + the (now-dead)
    # runner_id binding; capture it to prove the relaunch rotates it.
    stopped_conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert stopped_conv is not None
    original_runner_id = stopped_conv.runner_id

    # No runner client resolves now: the message path must take the
    # host-relaunch branch (no stop guard blocks it any more).
    set_runner_client(None)
    post_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
    )
    try:
        launch_frame = await _wait_for_launch(comm, budget_s=5.0)
    finally:
        # No runner ever connects, so cancel before the ~30s wait loop —
        # the relaunch frame + runner_id rotation already happened.
        post_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await post_task

    assert launch_frame is not None, (
        "a message to a stopped (non-sticky) host session must trigger "
        "host.launch_runner so the host re-spawns a runner; none arrived — "
        "a stale stop guard is still blocking the relaunch branch"
    )
    assert launch_frame.workspace == _WORKSPACE, (
        f"relaunch should carry the session workspace {_WORKSPACE!r}, "
        f"got {launch_frame.workspace!r}"
    )

    # The relaunch minted a fresh token-bound runner_id so the server stops
    # routing to the dead runner — same rotation the offline-runner path does.
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.runner_id is not None and conv.runner_id.startswith("runner_token_"), (
        f"relaunch should rebind a token-derived runner_id, got {conv.runner_id!r}"
    )
    assert conv.runner_id != original_runner_id, (
        "relaunch must mint a NEW runner_id (replace_runner_id); a stale id "
        "would keep routing messages to the dead runner"
    )


async def test_host_session_message_relaunches_offline_runner(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message to a host session whose runner is offline (but NOT
    deliberately stopped) asks the host to launch a fresh runner.

    This is the positive counterpart to
    ``test_stopped_host_session_message_does_not_relaunch_runner`` and the
    recovery path the inline launch's *lenient* contract relies on: when
    the initial host launch fails (``test_inline_launch_failure_still_
    returns_bound_session``) or the runner later dies, the session keeps
    its ``host_id`` + bound ``runner_id``, and the next message must
    re-spawn a runner via ``host.launch_runner`` rather than fail. The
    relaunch mints a fresh token-bound ``runner_id`` (``replace_runner_id``)
    so the server routes to the new runner, not the dead one.

    Mutation check: drop the relaunch branch (or its ``conv.host_id`` arm)
    in ``post_event`` and no launch frame is sent — ``_wait_for_launch``
    returns ``None`` and the first assertion fails. Make ``replace_runner_id``
    a no-op and the runner_id-rotation assertion fails.
    """
    from omnigent.runtime import set_runner_client
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0.0)

    comm = await _connect_host(app)
    session = await _inline_launch_session(client, comm)
    session_id = session["id"]
    original_runner_id = session["runner_id"]

    # Runner offline (no runner client resolves) and the session was never
    # stopped: the message path must take the host-relaunch branch.
    set_runner_client(None)
    post_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            },
        )
    )
    try:
        # The launch frame is sent (and the runner_id rotated) before the
        # route's 30s wait-for-runner loop, so a small budget suffices;
        # None means no relaunch fired.
        launch_frame = await _wait_for_launch(comm, budget_s=5.0)
    finally:
        # We never connect a runner, so the route would otherwise block
        # ~30s in its wait loop. Cancel now that we've observed (or missed)
        # the relaunch — the rotation + frame send already happened
        # synchronously, so the assertions below remain valid.
        post_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await post_task

    assert launch_frame is not None, (
        "a message to a host-bound, runner-offline, non-stopped session must "
        "trigger host.launch_runner so the host can re-spawn a runner; none "
        "arrived — the relaunch branch in post_event did not fire"
    )
    # Relaunch forwards the session's canonical workspace (not garbage),
    # proving it read the persisted workspace off the conversation row.
    assert launch_frame.workspace == _WORKSPACE, (
        f"relaunch should carry the session workspace {_WORKSPACE!r}, "
        f"got {launch_frame.workspace!r}"
    )

    # The relaunch minted a fresh token-bound runner_id via
    # replace_runner_id, distinct from the original binding, so the server
    # stops routing to the dead runner.
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.runner_id is not None and conv.runner_id.startswith("runner_token_"), (
        f"relaunch should rebind a token-derived runner_id, got {conv.runner_id!r}"
    )
    assert conv.runner_id != original_runner_id, (
        "relaunch must mint a NEW runner_id (replace_runner_id); a stale id "
        "would keep routing messages to the dead runner"
    )


async def test_host_session_message_waits_for_bound_runner_before_relaunch(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first message waits for the already-bound runner to register.

    The Web new-session path binds a token-derived ``runner_id`` and asks
    the host to spawn that runner before returning. The browser can post
    the initial message while the host runner process is alive but its
    tunnel is not registered yet. That must be treated as "runner still
    starting", not "runner is dead": the server should wait briefly for
    the existing binding to become routable, run the session-init
    handshake, and forward the message without sending a second
    ``host.launch_runner`` or rotating ``runner_id``.

    Mutation check: remove the grace wait before ``_launch_runner_on_host``
    and this test observes a second host launch frame plus a changed
    conversation ``runner_id``.
    """
    from omnigent.server.routes import sessions as sessions_module

    comm = await _connect_host(app)
    session = await _inline_launch_session(client, comm)
    session_id = session["id"]
    original_runner_id = session["runner_id"]

    runner_paths: list[str] = []
    init_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record runner POSTs and accept session init/message calls.

        :param request: Request the server sent to the runner, e.g.
            ``POST /v1/sessions`` or ``POST /v1/sessions/<id>/events``.
        :returns: A success response so the route can complete.
        """
        if request.method == "POST":
            runner_paths.append(request.url.path)
            if request.url.path == "/v1/sessions":
                init_bodies.append(json.loads(request.content))
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(200, json={})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    resolve_calls = {"n": 0}

    async def _staged_get_runner_client(sid: str, router: object) -> httpx.AsyncClient | None:
        """Simulate the pinned runner becoming routable during grace.

        :param sid: Session id being routed, e.g. ``"conv_abc123"``.
        :param router: Real app runner router (unused in this staged
            resolver).
        :returns: ``None`` first, then the fake runner client.
        """
        del router
        assert sid == session_id
        resolve_calls["n"] += 1
        return None if resolve_calls["n"] == 1 else fake_runner

    async def _noop_relay_ready(*args: Any, **kwargs: Any) -> None:
        """Stand in for ``_ensure_runner_relay_ready`` as a no-op.

        :param args: Ignored positional args from the call site.
        :param kwargs: Ignored keyword args from the call site.
        :returns: ``None``.
        """
        del args, kwargs

    monkeypatch.setattr(sessions_module, "_get_runner_client", _staged_get_runner_client)
    monkeypatch.setattr(sessions_module, "_ensure_runner_relay_ready", _noop_relay_ready)

    try:
        post_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                    },
                },
            )
        )
        await _wait_for_runner_connect_waiter(app, original_runner_id)
        app.state.tunnel_registry.register(original_runner_id, _NoopRunnerWS(), _runner_hello())
        resp = await post_task
    finally:
        await fake_runner.aclose()

    saw_launch = await _expect_no_launch(comm, budget_s=0.2)

    assert resp.status_code < 300, resp.text
    assert not saw_launch, (
        "the initial message should reuse the already-bound runner once it "
        "registers during the grace wait; a second host.launch_runner means "
        "the startup race still creates duplicate runners"
    )
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.runner_id == original_runner_id, (
        f"runner_id should stay on the originally launched runner "
        f"{original_runner_id!r}; got {conv.runner_id!r}"
    )
    assert app.state.tunnel_registry.connect_waiter_count(original_runner_id) == 0, (
        "connect waiter should be removed after the runner registers"
    )
    assert runner_paths and runner_paths[0] == "/v1/sessions", (
        f"the reused runner still needs session init before message forward; "
        f"recorded runner POSTs were {runner_paths!r}"
    )
    event_paths = [path for path in runner_paths if path.endswith("/events")]
    assert event_paths, (
        f"the user message should be forwarded after session init; "
        f"recorded runner POSTs were {runner_paths!r}"
    )
    assert init_bodies and init_bodies[0]["session_id"] == session_id, (
        f"session-init body should target {session_id!r}; got {init_bodies!r}"
    )


async def test_relaunch_posts_session_init_before_forwarding_message(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-relaunch path runs the session-init handshake (POST
    /v1/sessions) BEFORE forwarding the user's message to the runner.

    This is the fix for the host-restart stuck-bubble bug: a freshly
    relaunched runner has not run ``create_session`` yet, so for a
    native session its transcript forwarder is not watching. If the
    message is injected first, the round-trip that promotes the
    optimistic bubble and streams the reply never happens. Awaiting the
    handshake first guarantees the terminal + forwarder are in place
    before the message lands.

    The invariant is harness-agnostic — the handshake POST must precede
    the message forward for *any* relaunched session — so a plain agent
    suffices and keeps the assertion squarely on request ordering.

    ``_get_runner_client`` is staged offline→online to drive the
    relaunch branch deterministically: the app wires a real
    ``RunnerRouter`` that can only resolve a runner with a registered
    tunnel, so simulating "runner reconnects" via a registered tunnel
    would add unrelated plumbing. Staging the resolver returns ``None``
    first (relaunch fires) then the recording client (the relaunched
    runner is now reachable).

    Mutation check: drop the ``_ensure_runner_session_initialized`` call
    in ``post_event``'s relaunch branch and ``runner_paths`` loses its
    leading ``/v1/sessions`` POST (first assertion fails). Move it after
    the forward and the index-ordering assertion fails.
    """
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0.0)

    comm = await _connect_host(app)
    session = await _inline_launch_session(client, comm)
    session_id = session["id"]

    # Record runner POSTs in arrival order so we can assert the handshake
    # precedes the message forward.
    runner_paths: list[str] = []
    init_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record runner POSTs in arrival order and accept them.

        :param request: Request the server sent to the relaunched runner,
            e.g. a POST to ``/v1/sessions`` (handshake) or
            ``/v1/sessions/<id>/events`` (message forward).
        :returns: A 2xx so the server proceeds past each step.
        """
        if request.method == "POST":
            runner_paths.append(request.url.path)
            if request.url.path == "/v1/sessions":
                init_bodies.append(json.loads(request.content))
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(200, json={})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _staged_get_runner_client(sid: str, router: object) -> httpx.AsyncClient | None:
        """Return ``None`` so the route enters the relaunch branch.

        :param sid: Session id being routed (unused; one session here).
        :param router: Real app runner router (unused — staged here).
        :returns: ``None``.
        """
        del sid, router
        return None

    monkeypatch.setattr(sessions_module, "_get_runner_client", _staged_get_runner_client)

    async def _staged_wait_for_runner_client(
        session_id_arg: str,
        runner_router_arg: object,
        tunnel_registry_arg: object,
        *,
        runner_id: str | None,
        timeout_s: float,
        runner_exit_reports: object = None,
    ) -> httpx.AsyncClient | None:
        """Return the fake runner after the relaunch helper runs.

        :param session_id_arg: Session id being routed, e.g.
            ``"conv_abc123"``.
        :param runner_router_arg: Real app runner router (unused here).
        :param tunnel_registry_arg: Real app tunnel registry (unused here).
        :param runner_id: Relaunched runner id expected to connect.
        :param timeout_s: Wait budget requested by the route.
        :param runner_exit_reports: Crash-report store (unused here; the
            relaunched runner connects, so no conviction short-circuit).
        :returns: The recording fake runner client.
        """
        del runner_router_arg, tunnel_registry_arg, timeout_s, runner_exit_reports
        assert session_id_arg == session_id
        assert runner_id is not None and runner_id.startswith("runner_token_")
        return fake_runner

    monkeypatch.setattr(sessions_module, "_wait_for_runner_client", _staged_wait_for_runner_client)

    # Neutralize the SSE relay readiness wait: a MockTransport never
    # emits the relay's ready heartbeat, so the real wait would 5s-
    # timeout and 503. The relay is orthogonal to the ordering invariant
    # here and has its own coverage.
    async def _noop_relay_ready(*args: Any, **kwargs: Any) -> None:
        """Stand in for ``_ensure_runner_relay_ready`` as a no-op.

        :param args: Ignored positional args from the call site.
        :param kwargs: Ignored keyword args from the call site.
        :returns: ``None`` (no relay handle).
        """
        del args, kwargs

    monkeypatch.setattr(sessions_module, "_ensure_runner_relay_ready", _noop_relay_ready)

    try:
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            },
        )
    finally:
        # Always close the recording client, even if the POST raised, so a
        # regression can't leak the AsyncClient / emit unclosed-client warnings.
        await fake_runner.aclose()

    assert resp.status_code < 300, resp.text

    # Handshake must be recorded AND precede the first /events forward.
    # Pre-fix: no "/v1/sessions" entry at all. Wrong order: handshake
    # after the forward → the forwarder isn't watching when the message
    # lands, reproducing the stuck-bubble bug.
    assert "/v1/sessions" in runner_paths, (
        f"relaunch path must POST /v1/sessions (session-init handshake) to the "
        f"runner before forwarding; recorded runner POSTs were {runner_paths!r}"
    )
    event_paths = [p for p in runner_paths if p.endswith("/events")]
    assert event_paths, (
        f"the user message should still be forwarded to the runner's /events; "
        f"recorded runner POSTs were {runner_paths!r}"
    )
    assert runner_paths.index("/v1/sessions") < runner_paths.index(event_paths[0]), (
        f"session-init handshake must precede the message forward so the "
        f"transcript forwarder is watching first; got order {runner_paths!r}"
    )
    # The handshake targets this session (proves it's the real init call,
    # not an unrelated POST): the body carries the session id.
    assert init_bodies and init_bodies[0]["session_id"] == session_id, (
        f"handshake body should target session {session_id!r}; got {init_bodies!r}"
    )


async def test_codex_goal_relaunch_posts_session_init_before_goal_event(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening the Goal dialog on a slept Codex-native session runs the
    session-init handshake (POST /v1/sessions) on the woken runner BEFORE
    forwarding the goal control event.

    Codex goal state lives in the Codex app-server bridge, which only
    exists after ``create_session`` runs on the runner. When the Goal
    dialog wakes a host-bound session whose runner had slept, the goal
    route relaunches a runner and must run the session-init handshake
    first — otherwise the goal event lands on a runner with no bridge and
    is lost. This is the codex-goal sibling of
    ``test_relaunch_posts_session_init_before_forwarding_message``; the
    invariant (handshake precedes the forward) is the same, only the
    triggering path differs.

    The runner-resolution helpers are staged so the route deterministically
    enters its relaunch branch: no live runner, no already-bound runner,
    a successful host launch, and a relaunched runner that resolves to the
    recording client. ``_initialize_codex_goal_runner`` (and the handshake
    it drives) run for real against that client.

    Regression / mutation check: dropping the ``conversation_store`` arg
    from the ``_ensure_runner_session_initialized`` call in
    ``_initialize_codex_goal_runner`` (the exact pre-fix state) makes that
    call raise ``TypeError`` before any handshake POST — ``runner_paths``
    loses its ``/v1/sessions`` entry (first assertion fails) and the route
    500s. Moving the init after the goal-event forward fails the ordering
    assertion.
    """
    from omnigent._wrapper_labels import CODEX_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
    from omnigent.server.routes.codex import sessions as codex_sessions_module

    # Inline-launch a host-bound session (the goal relaunch path bails early
    # unless ``conv.host_id`` is set), then mark it codex-native so the goal
    # route accepts it (``_require_codex_native_goal_session`` keys off this
    # label). Its runner is treated as slept — the resolution helpers below
    # are staged to force the relaunch branch.
    comm = await _connect_host(app)
    session = await _inline_launch_session(client, comm)
    session_id = session["id"]
    SqlAlchemyConversationStore(db_uri).set_labels(
        session_id, {WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE}
    )

    # Record runner POSTs in arrival order so we can assert the handshake
    # precedes the goal-event forward.
    runner_paths: list[str] = []
    init_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record runner POSTs in arrival order and accept them.

        :param request: Request the server sent to the relaunched runner —
            a POST to ``/v1/sessions`` (handshake) or
            ``/v1/sessions/<id>/events`` (goal-event forward).
        :returns: A 2xx so the route proceeds past each step; the /events
            reply is a valid ``CodexGoalResponse`` body.
        """
        if request.method == "POST":
            runner_paths.append(request.url.path)
            if request.url.path == "/v1/sessions":
                init_bodies.append(json.loads(request.content))
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"goal": None})
        return httpx.Response(200, json={})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _no_live_runner(
        session_id_arg: str, runner_router_arg: object, event: dict[str, Any]
    ) -> None:
        """Return ``None`` so the goal route enters its relaunch branch.

        :param session_id_arg: Session being routed (unused; one session).
        :param runner_router_arg: App runner router (unused — staged here).
        :param event: Goal control event (unused; branch is unconditional).
        :returns: ``None``.
        """
        del session_id_arg, runner_router_arg, event

    async def _no_bound_runner(**kwargs: Any) -> None:
        """Report no already-bound runner so a relaunch is attempted.

        :param kwargs: Keyword args from the call site (unused).
        :returns: ``None``.
        """
        del kwargs

    async def _launched(**kwargs: Any) -> str:
        """Stand in for a successful host launch.

        :param kwargs: Keyword args from the call site (unused).
        :returns: The runner id expected to connect.
        """
        del kwargs
        return "runner_token_codexgoal"

    async def _relaunched_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        """Return the recording fake runner as the relaunched client.

        :param args: Positional args from the call site (unused).
        :param kwargs: Keyword args from the call site (unused).
        :returns: The recording fake runner client.
        """
        del args, kwargs
        return fake_runner

    monkeypatch.setattr(
        codex_sessions_module, "_forward_session_change_to_runner", _no_live_runner
    )
    monkeypatch.setattr(
        codex_sessions_module, "_wait_for_existing_codex_goal_runner", _no_bound_runner
    )
    monkeypatch.setattr(codex_sessions_module, "_start_codex_goal_runner_on_bound_host", _launched)
    monkeypatch.setattr(codex_sessions_module, "_wait_for_runner_client", _relaunched_client)

    try:
        resp = await client.put(
            f"/v1/sessions/{session_id}/codex_goal",
            json={"objective": "keep tests green"},
        )
    finally:
        # Always close the recording client so a regression can't leak the
        # AsyncClient / emit unclosed-client warnings.
        await fake_runner.aclose()

    assert resp.status_code < 300, resp.text

    # Handshake must be recorded AND precede the goal-event forward.
    # Pre-fix: the missing ``conversation_store`` arg raises TypeError
    # before the handshake POST, so there is no "/v1/sessions" entry.
    assert "/v1/sessions" in runner_paths, (
        f"goal relaunch must POST /v1/sessions (session-init handshake) to the "
        f"runner before forwarding the goal event; recorded runner POSTs were "
        f"{runner_paths!r}"
    )
    event_paths = [p for p in runner_paths if p.endswith("/events")]
    assert event_paths, (
        f"the goal control event should still be forwarded to the runner's "
        f"/events; recorded runner POSTs were {runner_paths!r}"
    )
    assert runner_paths.index("/v1/sessions") < runner_paths.index(event_paths[0]), (
        f"session-init handshake must precede the goal-event forward so the "
        f"Codex app-server bridge is loaded first; got order {runner_paths!r}"
    )
    # The handshake targets this session (proves it's the real init call).
    assert init_bodies and init_bodies[0]["session_id"] == session_id, (
        f"handshake body should target session {session_id!r}; got {init_bodies!r}"
    )


async def test_health_reports_online_for_host_on_other_replica(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    tmp_path,
    runtime_init: None,
) -> None:
    """``GET /health?session_ids=...`` reads host liveness from the DB, not the local registry.

    Multi-replica regression: the host's WebSocket is on replica A
    (the test ``app``) where the session is created. Then we query
    ``/health`` on a *separate* replica B that shares the same DB but
    has its own empty :class:`HostRegistry`. The runner tunnel lives
    on replica A's in-memory registry, so on replica B ``runner_online``
    is (correctly, under strict liveness) ``False`` — but ``host_online``
    must still be ``True``: the host is perfectly reachable on replica A,
    and host liveness is the cross-replica signal the open-session view
    keys off to decide "runner asleep, just send a message" vs "host
    offline". If the endpoint read host liveness from the per-replica
    registry instead of the DB, replica B would wrongly report
    ``host_online: false``.

    Mirrors the fix made in ``GET /v1/hosts``: the
    ``hosts`` DB row's ``status`` column is the cross-replica
    source of truth and is what ``_session_liveness`` /
    ``_bulk_session_liveness`` must consult for ``host_online``.
    """
    # Replica A owns the host WebSocket and creates the session.
    comm = await _connect_host(app)
    session = await _inline_launch_session(client, comm)
    session_id = session["id"]

    # Replica B: same DB, fresh app/registries. It never sees the
    # host's WebSocket — only the persisted ``hosts`` row.
    artifact_store_b = LocalArtifactStore(str(tmp_path / "artifacts_b"))
    app_b = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store_b,
        agent_cache=AgentCache(
            artifact_store=artifact_store_b,
            cache_dir=tmp_path / "cache_b",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
    )
    assert app_b.state.host_registry.get(_HOST_ID) is None, (
        "test setup is broken: replica B's host registry should be empty."
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b),
        base_url="http://test",
    ) as client_b:
        single = await client_b.get(f"/health?session_id={session_id}")
        batch = await client_b.get(f"/health?session_ids={session_id}")

    assert single.status_code == 200
    # The runner tunnel is on replica A only, so strict runner_online is
    # False here — but host_online must be True, read cross-replica from
    # the hosts DB row rather than replica B's empty local registry.
    assert single.json()["session"]["host_online"] is True, (
        "replica B reported the host offline for a host connected to "
        "replica A — _session_liveness is reading host liveness from the "
        "local registry instead of the hosts DB."
    )
    assert single.json()["session"]["runner_online"] is False
    assert batch.status_code == 200
    assert batch.json()["sessions"][session_id]["host_online"] is True, (
        "replica B reported the host offline in the batch path — "
        "_bulk_session_liveness is reading host liveness from the local "
        "registry instead of the hosts DB."
    )
    assert batch.json()["sessions"][session_id]["runner_online"] is False
